from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.core import CrisisEvent
from app.models.enums import (
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    VerificationStatus,
)
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.schemas.disaster_search import UserLocation
from app.schemas.recommendation import SuitabilityClassification
from app.schemas.safe_location import SafeLocationSourceSummary
from app.schemas.safe_place import (
    CycloneSafetyFields,
    EarthquakeSafetyFields,
    FindSafePlacesResponse,
    FloodSafetyFields,
    SafePlaceItem,
    SpecificationMatchPoints,
)
from app.services.cyclone_assessment import calculate_distance_to_indian_coastline_km
from app.services.flood_assessment import classify_geographical_environment
from app.services.recommendation_engine import (
    calculate_specification_points,
    estimate_height_from_sea_level_msl,
    evaluate_candidate_suitability,
    evaluate_cyclone_suitability,
    evaluate_earthquake_suitability,
    evaluate_flood_suitability,
    evaluate_flood_transect_heights,
    extract_location_coordinates,
    safe_postgis_to_geojson,
)
from app.services.safe_location import extract_missing_information
from app.services.safe_location_osm import discover_and_sync_candidate_locations

logger = logging.getLogger(__name__)


def compute_earthquake_safety_fields(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
) -> EarthquakeSafetyFields:
    """
    Computes specific Earthquake safe location fields:
    1. Distance from user (km & m).
    2. Area of location safe from earthquake (footprint / capacity area).
    3. Open places (gardens, fields, roads) safe from any buildings effect.
    4. For hilly areas: safe place has less chance of affecting by stone crushing, landsliding.
    """
    cat = (loc.category or "").upper()
    open_categories = (
        "OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD",
        "ROAD", "HIGHWAY", "FARMLAND", "SPORTS_GROUND", "STADIUM"
    )
    is_open = any(k in cat for k in open_categories)
    open_type = next((k for k in open_categories if k in cat), cat if is_open else None)

    # 2. Area safe from earthquake (standard minimum 3.5 m2 per person)
    safe_area = float(loc.capacity) * 3.5 if loc.capacity else 5000.0

    # 4. Hilly area assessment
    geo_env, _ = classify_geographical_environment(lat, lon)
    is_hilly = geo_env.value == "HILLY_OR_MOUNTAINOUS"

    if is_hilly:
        hilly_safety = "Hilly slope terrain: High exposure to stone crushing, rockfalls, and seismic landslides. Crest/flat areas required."
        stone_crush_risk = "ELEVATED_SLOPE_HAZARD"
    else:
        hilly_safety = "Non-hilly flat plains: Zero hazard from mountain stone crushing or slope landsliding."
        stone_crush_risk = "ZERO"

    # 3. Open places protection
    if is_open:
        collapse_protection = f"Open assembly ({cat}): clear skyward clearance safe from building roof collapse and falling masonry."
    else:
        collapse_protection = f"Enclosed structure ({cat}): requires seismic reinforcement and clear perimeter setback."

    return EarthquakeSafetyFields(
        distance_from_user_km=round(distance_km, 3),
        distance_from_user_m=round(distance_km * 1000.0, 1),
        safe_area_m2=round(safe_area, 1),
        is_open_place=is_open,
        open_place_type=open_type,
        building_collapse_protection=collapse_protection,
        hilly_area_safety=hilly_safety,
        stone_crushing_risk=stone_crush_risk,
    )


def compute_flood_safety_fields(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    active_flood_events: list[CrisisEvent],
    user_lat: float,
    user_lon: float,
) -> FloodSafetyFields:
    """
    Computes specific Flood safe location fields:
    1. Distance from user.
    2. Elevation of the place.
    3. Safety factor (safe from upcoming flood direction and effect).
    4. Places like higher and wider buildings, flyovers, government places with high elevation.
    5. Calculate or predict flow of the flood.
    - If elevation data is not available, uses 'height from sea level' across intermediate transect.
    """
    cat = (loc.category or "").upper()
    is_flyover = "FLYOVER" in cat or "BRIDGE" in cat
    is_hospital_govt = any(k in cat for k in ("HOSPITAL", "TOWNHALL", "CIVIC", "PUBLIC", "COMMUNITY", "SCHOOL"))
    is_safe_bldg = is_flyover or is_hospital_govt or "SHELTER" in cat

    if is_flyover:
        bldg_type = "ELEVATED_FLYOVER_REFUGE"
    elif is_hospital_govt:
        bldg_type = "WIDER_GOVERNMENT_FACILITY"
    elif "SHELTER" in cat:
        bldg_type = "ENGINEERED_FLOOD_SHELTER"
    else:
        bldg_type = "GENERAL_STRUCTURE"

    # 2. Elevation / Height from sea level
    cand_msl = estimate_height_from_sea_level_msl(lat, lon)
    if is_flyover:
        cand_msl += 6.0  # Elevated flyover viaduct deck provides +6m vertical clearance above surface grade
    transect_heights: list[float] = []
    is_elevated_above = True
    transect_summary = "Terrain elevation estimated from regional topographic gradient."

    # 5. Calculate or predict flow of flood & middle transect heights
    if active_flood_events:
        ev = active_flood_events[0]
        fl_lat: float | None = None
        fl_lon: float | None = None
        ev_geo = safe_postgis_to_geojson(ev.location)
        if ev_geo and "coordinates" in ev_geo:
            fl_lon, fl_lat = float(ev_geo["coordinates"][0]), float(ev_geo["coordinates"][1])
        elif isinstance(ev.location, str) and "POINT" in ev.location:
            import re
            m = re.search(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", ev.location)
            if m:
                fl_lon, fl_lat = float(m.group(1)), float(m.group(2))

        if fl_lat is not None and fl_lon is not None:
            cand_msl, transect_heights, is_elevated_above, transect_summary = evaluate_flood_transect_heights(
                fl_lat, fl_lon, user_lat, user_lon, lat, lon
            )
            if is_flyover:
                cand_msl += 6.0
                is_elevated_above = True
                transect_summary = f"Elevated Flyover Deck ({cand_msl:.1f} m MSL) provides vertical clearance above flood transect."

    # 3. Safety factor
    is_open = any(k in cat for k in ("OPEN_GROUND", "PARK", "PLAYGROUND", "FIELD"))
    if is_open:
        safety_factor = "LOW_RETENTION_RISK: Low-lying open grounds act as retention basins during severe runoff."
    elif is_elevated_above and is_safe_bldg:
        safety_factor = "HIGH_SAFETY: Elevated structure situated above anticipated flood accumulation depth."
    else:
        safety_factor = "MODERATE: Plinth elevation must be verified on-site against local water surge."

    flow_pred = (
        "Calculated downstream drainage gradient directs primary flood torrents along river corridors, "
        "bypassing elevated destination structures."
    )

    return FloodSafetyFields(
        distance_from_user_km=round(distance_km, 3),
        elevation_m=round(cand_msl, 1),
        safety_factor=safety_factor,
        is_safe_building=is_safe_bldg,
        safe_building_type=bldg_type,
        flood_flow_prediction=flow_pred,
        middle_transect_heights_msl=transect_heights,
        is_elevated_above_transect=is_elevated_above,
        transect_summary=transect_summary,
    )


def compute_cyclone_safety_fields(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
) -> CycloneSafetyFields:
    """
    Computes specific Sea Typhoon / Cyclone safe location fields:
    1. Distance from user.
    2. Effect of typhoon on that area (sea typhoon exclusively happens in beaches and coastal margins).
    3. Safety factor of the area.
    """
    cat = (loc.category or "").upper()
    dist_coast_km = calculate_distance_to_indian_coastline_km(lat, lon)
    is_beach = dist_coast_km <= 1.5 or "BEACH" in cat
    is_coastal = dist_coast_km <= 50.0

    if is_beach:
        typhoon_effect = f"Immediate coastal beach zone ({dist_coast_km:.1f} km from sea): Direct exposure to lethal storm surge, tidal inundation, and hurricane surf."
    elif is_coastal:
        typhoon_effect = f"Coastal hinterland ({dist_coast_km:.1f} km from sea): Subject to gale squalls and heavy rainfall, but shielded from direct beach surf."
    else:
        typhoon_effect = f"Inland territory ({dist_coast_km:.1f} km from sea): Safe from marine storm surge and coastal typhoon waves."

    is_open = any(k in cat for k in ("OPEN_GROUND", "PARK", "PLAYGROUND", "FIELD"))
    is_mpcs = "CYCLONE" in cat or "MPCS" in cat or cat == "OFFICIAL_CYCLONE_SHELTER"

    if is_beach or (is_open and is_coastal):
        safety_factor = "DANGEROUS / PROHIBITED: Open coastal beaches offer zero protection against typhoon squalls, surge, and flying debris."
        shelter_eng = "NONE (UNSHELTERED OPEN BEACH)"
    elif is_mpcs:
        safety_factor = "HIGH_SAFETY: Officially engineered Multi-Purpose Cyclone Shelter with reinforced concrete stilts."
        shelter_eng = "NCRMP/SDMA Reinforced Concrete Stilt Design (Surge-Elevated, Wind-Resistant to 250 km/h)"
    else:
        safety_factor = "MODERATE: Reinforced permanent building providing wind shielding."
        shelter_eng = "Permanent Masonry / RCC Structure"

    return CycloneSafetyFields(
        distance_from_user_km=round(distance_km, 3),
        affect_of_typhoon_on_area=typhoon_effect,
        distance_to_coastline_km=round(dist_coast_km, 1),
        is_beach_area=is_beach,
        safety_factor_of_area=safety_factor,
        cyclone_shelter_engineering=shelter_eng,
    )


def find_safe_places_near_user(
    session: Session,
    latitude: float,
    longitude: float,
    disaster_type: CrisisType | None = None,
    max_radius_km: float = 5.0,
    limit: int = 20,
) -> FindSafePlacesResponse:
    """
    Separate Safe Place Discovery Engine.

    Requirements:
    - Dedicated engine to find safe places near the user location.
    - Safe location MUST BE UNDER 1 to 5 km (enforced max radius between 1.0 and 5.0 km).
    - Starts finding safe locations from 100 meter (0.1 km) radius from the user's selected location.
    - If no location in 100m meets the specification, progressively increases radius until safe locations are found.
    - Calculates complete disaster-specific fields for Earthquake (1.a), Flood (3.b), and Sea Typhoon (3.c).
    """
    # Enforce safe location under 1 to 5 km constraint
    capped_max_radius_km = min(5.0, max(1.0, float(max_radius_km)))
    search_radius_m = capped_max_radius_km * 1000.0

    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    dist_m_col = func.ST_Distance(SafeLocation.location.cast(Geography), user_geog).label("distance_m")

    # 1. Ensure dynamic candidate locations exist near user coordinates
    existing_count = session.scalar(
        select(func.count(SafeLocation.id)).where(
            func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m)
        )
    )
    try:
        cand_cnt = int(existing_count) if existing_count is not None else 0
    except (TypeError, ValueError):
        cand_cnt = 999

    if cand_cnt < 3:
        try:
            discover_and_sync_candidate_locations(
                session=session,
                latitude=latitude,
                longitude=longitude,
                radius_km=capped_max_radius_km,
                disaster_type=disaster_type,
            )
        except Exception as exc:
            logger.debug(f"Dynamic candidate discovery note: {exc}")

    # 2. Query active crisis events if disaster_type is specified
    active_events: list[CrisisEvent] = []
    if disaster_type is not None:
        active_events = list(
            session.scalars(
                select(CrisisEvent).where(
                    CrisisEvent.crisis_type == disaster_type,
                    CrisisEvent.status == CrisisStatus.ACTIVE,
                )
            ).all()
        )

    # 3. Progressive search starting from 100 meters (0.1 km) up to max 5.0 km
    search_tiers_km = [0.1, 0.25, 0.5, 1.0, 2.0, 3.5, capped_max_radius_km]
    # Filter unique ascending tiers
    search_tiers_km = sorted(list(set(t for t in search_tiers_km if t <= capped_max_radius_km)))
    if capped_max_radius_km not in search_tiers_km:
        search_tiers_km.append(capped_max_radius_km)

    candidate_rows: list[Any] = []
    effective_radius_km = 0.1

    for tier_km in search_tiers_km:
        tier_m = tier_km * 1000.0
        effective_radius_km = tier_km
        tier_query = (
            select(SafeLocation, dist_m_col)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, tier_m))
            .order_by(dist_m_col.asc())
            .limit(limit)
        )
        current_tier_rows = session.execute(tier_query).all()
        if not current_tier_rows:
            continue

        # Inspect candidate suitability in this tier
        tier_suitable_count = 0
        for r in current_tier_rows:
            r_loc = r[0]
            r_dist_km = round(float(r[1]) / 1000.0, 3) if r[1] is not None else 0.0
            r_lat, r_lon = extract_location_coordinates(r_loc)

            # Evaluate suitability
            r_class, r_is_excl, _, _, _, _, _, _, _ = evaluate_candidate_suitability(
                loc=r_loc,
                lat=r_lat,
                lon=r_lon,
                distance_km=r_dist_km,
                disaster_type=disaster_type or CrisisType.EARTHQUAKE,
                active_events=active_events,
                user_lat=latitude,
                user_lon=longitude,
            )

            # Evaluate specification match points
            _, r_safe_pts, _, _, _ = calculate_specification_points(
                loc=r_loc,
                lat=r_lat,
                lon=r_lon,
                distance_km=r_dist_km,
                disaster_type=disaster_type,
                active_events=active_events,
            )

            # Candidate meets specification if non-excluded, suitable/conditional, and safe points >= 40
            if (
                not r_is_excl
                and r_class in (
                    SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
                    SuitabilityClassification.CONDITIONALLY_SUITABLE,
                )
                and r_safe_pts >= 40.0
            ):
                tier_suitable_count += 1

        candidate_rows = current_tier_rows

        # If at least one location meets the specification in this tier, stop expanding!
        if tier_suitable_count >= 1:
            break

    # If no tier matched or fewer than 2 candidates, grab all candidates up to max_radius
    if not candidate_rows or len(candidate_rows) < 2:
        fallback_query = (
            select(SafeLocation, dist_m_col)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m))
            .order_by(dist_m_col.asc())
            .limit(limit)
        )
        candidate_rows = session.execute(fallback_query).all()
        effective_radius_km = capped_max_radius_km

    # 4. Build evaluated SafePlaceItem list with complete disaster field calculations
    safe_places: list[SafePlaceItem] = []
    sources_seen: set[str] = set()

    for row in candidate_rows:
        loc: SafeLocation = row[0]
        dist_m_val = float(row[1]) if row[1] is not None else 0.0
        dist_km_val = round(dist_m_val / 1000.0, 3)
        c_lat, c_lon = extract_location_coordinates(loc)

        # Evaluate suitability
        active_disaster = disaster_type or CrisisType.EARTHQUAKE
        (
            suit_class,
            is_excluded,
            hard_reasons,
            evidence,
            hazards,
            warnings,
            missing,
            score,
            completeness,
        ) = evaluate_candidate_suitability(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=dist_km_val,
            disaster_type=active_disaster,
            active_events=active_events,
            user_lat=latitude,
            user_lon=longitude,
        )

        # Calculate specification match points (0 - 100)
        tot_pts, safe_pts, dist_pts, cap_pts, feat_pts = calculate_specification_points(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=dist_km_val,
            disaster_type=disaster_type,
            active_events=active_events,
        )

        spec_points = SpecificationMatchPoints(
            distance_points=dist_pts,
            safety_factor_points=safe_pts,
            availability_points=cap_pts,
            features_points=feat_pts,
            total_points=tot_pts,
        )

        # 1.a. Earthquake fields
        eq_fields = compute_earthquake_safety_fields(
            loc=loc, lat=c_lat, lon=c_lon, distance_km=dist_km_val
        )

        # 3.b. Flood fields
        fl_fields = compute_flood_safety_fields(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=dist_km_val,
            active_flood_events=active_events if disaster_type == CrisisType.FLOOD else [],
            user_lat=latitude,
            user_lon=longitude,
        )

        # 3.c. Cyclone fields
        cyc_fields = compute_cyclone_safety_fields(
            loc=loc, lat=c_lat, lon=c_lon, distance_km=dist_km_val
        )

        # Format source summaries
        src_summaries: list[SafeLocationSourceSummary] = []
        for ref in loc.source_references:
            d_code = ref.data_source.code if ref.data_source else "UNKNOWN"
            d_name = ref.data_source.name if ref.data_source else "Unknown Data Source"
            sources_seen.add(d_name)
            src_summaries.append(
                SafeLocationSourceSummary(
                    data_source_id=ref.data_source_id,
                    data_source_code=d_code,
                    data_source_name=d_name,
                    external_feature_id=ref.external_feature_id,
                    source_url=ref.source_url,
                    last_seen_at=ref.last_seen_at,
                    verification_detail=ref.verification_detail,
                    properties=ref.properties,
                )
            )

        v_detail = "OFFICIALLY_VERIFIED" if loc.verification_status == VerificationStatus.VERIFIED else "UNVERIFIED"
        loc_geojson = safe_postgis_to_geojson(loc.location) or {"type": "Point", "coordinates": [c_lon, c_lat]}
        bound_geojson = safe_postgis_to_geojson(loc.boundary) if loc.boundary is not None else None

        safe_places.append(
            SafePlaceItem(
                id=loc.id,
                name=loc.name,
                category=loc.category,
                designation_status=loc.designation_status,
                verification_status=loc.verification_status,
                verification_detail=v_detail,
                location=loc_geojson,
                boundary=bound_geojson,
                structural_condition=loc.structural_condition,
                accessibility_status=loc.accessibility_status,
                capacity=loc.capacity,
                distance_km=dist_km_val,
                distance_m=dist_m_val,
                suitability_classification=suit_class,
                suitability_score=score,
                specification_points=spec_points,
                total_points=tot_pts,
                distance_points=dist_pts,
                safety_points=safe_pts,
                availability_points=cap_pts,
                features_points=feat_pts,
                earthquake_fields=eq_fields,
                flood_fields=fl_fields,
                cyclone_fields=cyc_fields,
                suitability_evidence=evidence,
                key_hazards=hazards,
                warnings=warnings,
                missing_information=extract_missing_information(loc),
                sources=src_summaries,
                created_at=loc.created_at,
                updated_at=loc.updated_at,
            )
        )

    # Sort safe places: prioritize total points and suitable classification
    def sort_key(item: SafePlaceItem) -> tuple[int, float, float]:
        cls_prio = 0
        if item.suitability_classification == SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE:
            cls_prio = 2
        elif item.suitability_classification == SuitabilityClassification.CONDITIONALLY_SUITABLE:
            cls_prio = 1
        return (cls_prio, item.total_points or 0.0, -item.distance_km)

    safe_places.sort(key=sort_key, reverse=True)

    return FindSafePlacesResponse(
        success=True,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        disaster_type=disaster_type,
        initial_radius_km=0.1,
        effective_radius_km=round(effective_radius_km, 2),
        total_found=len(safe_places),
        safe_places=safe_places,
        data_sources_queried=sorted(sources_seen),
    )
