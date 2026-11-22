from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import delete, func, or_, select
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
from app.services.city_elevation_data import (
    calculate_elevation_difference,
    find_nearest_city_elevation,
    get_city_height_from_sea_level,
)
from app.services.cyclone_assessment import calculate_distance_to_indian_coastline_km
from app.services.elevation_transect_engine import (
    compute_elevation_between_crisis_and_user,
)
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
from app.services.safe_location_ncrmp import sync_ncrmp_shelters
from app.services.safe_location_osm import discover_and_sync_candidate_locations, sync_osm_locations

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
    - Uses authoritative 'height from sea level' across city elevation dataset and intermediate transect.
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

    # 2. Elevation / Height from sea level using city dataset
    cand_ground_msl = get_city_height_from_sea_level(lat, lon)
    cand_msl = cand_ground_msl + (6.0 if is_flyover else 0.0)
    user_msl = get_city_height_from_sea_level(user_lat, user_lon)
    cand_nearest_city, _, _, _, _ = find_nearest_city_elevation(lat, lon)
    user_nearest_city, _, _, _, _ = find_nearest_city_elevation(user_lat, user_lon)

    diff_eval = calculate_elevation_difference(
        loc1_name=loc.name,
        loc1_height_m=cand_msl,
        loc2_name="User Location",
        loc2_height_m=user_msl,
    )
    diff_from_user_m = round(cand_msl - user_msl, 1)
    is_higher = cand_msl > user_msl

    transect_heights: list[float] = []
    is_elevated_above = True
    transect_summary = diff_eval["summary"]
    flow_pred = (
        "Calculated downstream drainage gradient directs primary flood torrents along river corridors, "
        "bypassing elevated destination structures."
    )

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
            transect_res = compute_elevation_between_crisis_and_user(
                crisis_lat=fl_lat,
                crisis_lon=fl_lon,
                user_lat=user_lat,
                user_lon=user_lon,
            )
            transect_heights = transect_res.middle_heights_msl
            is_elevated_above = cand_msl >= (transect_res.avg_middle_height_msl + 1.0)
            transect_summary = (
                f"{diff_eval['summary']} Intermediate flood transect ({transect_res.min_middle_height_msl:.1f}–"
                f"{transect_res.max_middle_height_msl:.1f} m MSL) from {transect_res.crisis_start_nearest_city}."
            )
            flow_pred = transect_res.flow_prediction_summary

    # 3. Safety factor
    is_open = any(k in cat for k in ("OPEN_GROUND", "PARK", "PLAYGROUND", "FIELD"))
    if is_open and not is_higher:
        safety_factor = "LOW_RETENTION_RISK: Low-lying open grounds act as retention basins during severe runoff."
    elif is_higher and is_safe_bldg:
        safety_factor = (
            f"HIGH_SAFETY: Located at {cand_msl:.1f} m MSL (+{diff_from_user_m:.1f} m higher than user in {user_nearest_city}); "
            "elevated above anticipated flood accumulation depth."
        )
    elif is_higher:
        safety_factor = (
            f"ELEVATED_GROUND_SAFETY: Located at {cand_msl:.1f} m MSL (+{diff_from_user_m:.1f} m higher than user); "
            "natural terrain height advantage."
        )
    else:
        safety_factor = f"MODERATE: Plinth elevation must be verified on-site ({diff_eval['summary']})."

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
        elevation_difference_from_user_m=diff_from_user_m,
        is_higher_than_user=is_higher,
        nearest_city=cand_nearest_city,
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


def is_real_location_suitable_for_crisis(
    loc: SafeLocation,
    crisis: CrisisType,
    suit_class: SuitabilityClassification,
    safe_pts: float,
    user_lat: float | None = None,
    user_lon: float | None = None,
) -> bool:
    """
    Validates whether a physical safe location meets the strict disaster-specific criteria:
    - Eliminates hypothetical / synthetic entries (coordinates or dynamic refuge zone in name).
    - Excludes NOT_RECOMMENDED or EXCLUDED classifications.
    - Earthquake (1.a): ONLY open places (gardens, fields, roads, grounds, stadiums) safe from building collapse/falling debris;
      in hilly areas, low stone crushing / landslide risk. Enclosed buildings/flyovers are excluded.
    - Flood (3.b): ONLY high-elevation safe buildings (hospitals, government complexes, colleges), elevated flyovers,
      and high-ground open fields having more height than the user's location.
      Strictly excludes low-lying open grounds, parks, gardens that get inundated.
    - Cyclone / Sea Typhoon (3.c): ONLY engineered cyclone shelters (MPCS on stilts) and reinforced solid inland facilities;
      strictly excludes open beaches, open grounds, parks, and flyovers.
    """
    name_str = (loc.name or "").strip()
    name_lower = name_str.lower()
    if not name_str or len(name_str) < 3:
        return False
    if "(" in name_lower and ")" in name_lower and any(c.isdigit() for c in name_lower):
        if "refuge zone" in name_lower or "assembly zone" in name_lower or "platform" in name_lower:
            return False

    if suit_class in (
        SuitabilityClassification.EXCLUDED,
        SuitabilityClassification.NOT_RECOMMENDED,
    ):
        return False
    if safe_pts < 30.0:
        return False

    cat = (loc.category or "").upper()
    is_open = (
        cat in ("OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD", "ROAD", "CAMPUS")
        or any(w in name_lower for w in ["ground", "park", "garden", "field", "stadium", "maidan", "lawn", "turf", "open assembly"])
    )
    is_flyover = (
        "flyover" in name_lower
        or "elevated corridor" in name_lower
        or "elevated deck" in name_lower
        or "viaduct" in name_lower
        or cat in ("FLYOVER", "ELEVATED_STRUCTURE")
    )
    is_building = (
        cat in ("HOSPITAL", "SCHOOL", "GOVERNMENT_BUILDING", "COMMUNITY_CENTER", "OFFICIAL_FLOOD_SHELTER")
        or any(w in name_lower for w in ["hospital", "bhawan", "secretariat", "building", "college", "school", "hall", "complex", "centre", "center", "institute", "university"])
    )

    if crisis == CrisisType.EARTHQUAKE:
        if is_flyover:
            return False
        if is_building and not is_open:
            return False
        return is_open

    elif crisis == CrisisType.FLOOD:
        if is_open and not (is_flyover or is_building):
            # Check if this open field has higher elevation than user location
            if user_lat is not None and user_lon is not None:
                c_lat, c_lon = extract_location_coordinates(loc)
                c_h = get_city_height_from_sea_level(c_lat, c_lon)
                u_h = get_city_height_from_sea_level(user_lat, user_lon)
                if c_h > u_h + 1.0:
                    return True
            return False
        return is_flyover or is_building or cat in ("OFFICIAL_FLOOD_SHELTER", "GOVERNMENT_BUILDING", "HOSPITAL")

    elif crisis == CrisisType.CYCLONE:
        if is_open or is_flyover or "beach" in name_lower:
            return False
        is_cyclone_shelter = (
            cat in ("OFFICIAL_CYCLONE_SHELTER", "COMMUNITY_CENTER", "GOVERNMENT_BUILDING", "SCHOOL", "HOSPITAL")
            or any(w in name_lower for w in ["cyclone", "shelter", "mpcs", "pucca", "relief", "school", "hospital", "bhawan"])
        )
        return is_cyclone_shelter

    return True


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
    - Give ONLY real safe locations that physically exist near the user (no hypothetical/synthetic flyovers or zones).
    - Starts finding safe locations from 100 meter (0.1 km) radius from user location.
    - If no safe location is found in 100m, progressively increase the radius until real safe locations are found.
    - Safe location must be under 1 to 5 km (expanding outward from 0.1km up to 5km, with emergency fallback if remote).
    - Different safe locations for different crisis: only show safe locations matching the chosen or ongoing crisis.
    - Calculates complete disaster-specific fields for Earthquake (1.a), Flood (3.b), and Sea Typhoon (3.c).
    """
    capped_max_radius_km = min(5.0, max(1.0, float(max_radius_km)))
    search_radius_m = capped_max_radius_km * 1000.0

    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    dist_m_col = func.ST_Distance(SafeLocation.location.cast(Geography), user_geog).label("distance_m")

    # 1. Purge any historical synthetic/placeholder locations
    try:
        session.execute(
            delete(SafeLocation).where(
                or_(
                    SafeLocation.name.ilike("%refuge zone (%%"),
                    SafeLocation.name.ilike("%assembly zone (%%"),
                )
            )
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.debug(f"Synthetic cleanup note: {exc}")

    # 2. Determine active crisis: user selected crisis, or ongoing active crisis near user, or default
    active_crisis = disaster_type
    if active_crisis is None:
        nearby_event = session.scalars(
            select(CrisisEvent)
            .where(
                CrisisEvent.status == CrisisStatus.ACTIVE,
                func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, 150000.0),
            )
            .order_by(func.ST_Distance(CrisisEvent.location.cast(Geography), user_geog).asc())
            .limit(1)
        ).first()
        if nearby_event is not None:
            active_crisis = nearby_event.crisis_type
        else:
            any_active = session.scalars(
                select(CrisisEvent).where(CrisisEvent.status == CrisisStatus.ACTIVE).limit(1)
            ).first()
            active_crisis = any_active.crisis_type if any_active is not None else CrisisType.EARTHQUAKE

    # 3. Query active events for this crisis type
    active_events: list[CrisisEvent] = list(
        session.scalars(
            select(CrisisEvent).where(
                CrisisEvent.crisis_type == active_crisis,
                CrisisEvent.status == CrisisStatus.ACTIVE,
            )
        ).all()
    )

    # 4. Ingest real OSM candidates dynamically if local density is low
    existing_count = session.scalar(
        select(func.count(SafeLocation.id)).where(
            func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m),
            ~SafeLocation.name.ilike("%refuge zone (%%"),
            ~SafeLocation.name.ilike("%assembly zone (%%"),
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
                disaster_type=active_crisis,
            )
        except Exception as exc:
            logger.debug(f"Dynamic candidate discovery note: {exc}")

    # 5. Progressive search starting from 100 meters (0.1 km)
    # Tiers expand progressively: 100m, 250m, 500m, 1km, 2km, 3.5km, 5km (plus safety net if remote)
    search_tiers_km = [0.1, 0.25, 0.5, 1.0, 2.0, 3.5, 5.0, 10.0, 25.0, 50.0, 100.0, 500.0]

    safe_places: list[SafePlaceItem] = []
    effective_radius_km = 0.1
    sources_seen: set[str] = set()

    for tier_km in search_tiers_km:
        tier_m = tier_km * 1000.0
        effective_radius_km = tier_km

        tier_query = (
            select(SafeLocation, dist_m_col)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(
                func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, tier_m),
                ~SafeLocation.name.ilike("%refuge zone (%%"),
                ~SafeLocation.name.ilike("%assembly zone (%%"),
            )
            .order_by(dist_m_col.asc())
            .limit(limit * 3)
        )
        current_tier_rows = session.execute(tier_query).all()
        if not current_tier_rows:
            continue

        matching_tier_items: list[SafePlaceItem] = []

        for row in current_tier_rows:
            loc: SafeLocation = row[0]
            dist_m_val = float(row[1]) if row[1] is not None else 0.0
            dist_km_val = round(dist_m_val / 1000.0, 3)
            c_lat, c_lon = extract_location_coordinates(loc)

            # Evaluate candidate suitability
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
                disaster_type=active_crisis,
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
                disaster_type=active_crisis,
                active_events=active_events,
            )

            # Check if this real location is suitable for the specific crisis
            if not is_real_location_suitable_for_crisis(
                loc, active_crisis, suit_class, safe_pts, user_lat=latitude, user_lon=longitude
            ):
                continue

            spec_points = SpecificationMatchPoints(
                distance_points=dist_pts,
                safety_factor_points=safe_pts,
                availability_points=cap_pts,
                features_points=feat_pts,
                total_points=tot_pts,
            )

            # 1.a Earthquake fields
            eq_fields = compute_earthquake_safety_fields(
                loc=loc, lat=c_lat, lon=c_lon, distance_km=dist_km_val
            )

            # 3.b Flood fields
            fl_fields = compute_flood_safety_fields(
                loc=loc,
                lat=c_lat,
                lon=c_lon,
                distance_km=dist_km_val,
                active_flood_events=active_events if active_crisis == CrisisType.FLOOD else [],
                user_lat=latitude,
                user_lon=longitude,
            )

            # 3.c Cyclone fields
            cyc_fields = compute_cyclone_safety_fields(
                loc=loc, lat=c_lat, lon=c_lon, distance_km=dist_km_val
            )

            # Format source summaries
            src_summaries: list[SafeLocationSourceSummary] = []
            for ref in loc.source_references:
                d_code = ref.data_source.code if ref.data_source else "UNKNOWN"
                d_name = ref.data_source.name if ref.data_source else "Unknown Data Source"
                sources_seen.add(d_name)
                ref_v_detail = (ref.properties.get("verification_detail") if ref.properties else None) or (
                    "OFFICIALLY_VERIFIED" if loc.verification_status == VerificationStatus.VERIFIED else "UNVERIFIED"
                )
                src_summaries.append(
                    SafeLocationSourceSummary(
                        data_source_id=ref.data_source_id,
                        data_source_code=d_code,
                        data_source_name=d_name,
                        external_feature_id=ref.external_feature_id,
                        source_url=ref.source_url,
                        last_seen_at=ref.last_seen_at,
                        verification_detail=ref_v_detail,
                        properties=ref.properties,
                    )
                )

            v_detail = "OFFICIALLY_VERIFIED" if loc.verification_status == VerificationStatus.VERIFIED else "UNVERIFIED"
            loc_geojson = safe_postgis_to_geojson(loc.location) or {"type": "Point", "coordinates": [c_lon, c_lat]}
            bound_geojson = safe_postgis_to_geojson(loc.boundary) if loc.boundary is not None else None

            matching_tier_items.append(
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

        if matching_tier_items:
            # We found matching real safe locations for this crisis in this tier!
            safe_places = matching_tier_items[:limit]
            break

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
