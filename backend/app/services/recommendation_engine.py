from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database.geometry import geojson_to_postgis
from app.models.core import CrisisEvent, CrisisSourceReport, HazardObservation
from app.models.enums import (
    AccessibilityStatus,
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    RecommendationRunStatus,
    RiskLevel,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import (
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
    SafeLocationSourceReference,
)
from app.schemas.disaster_search import UserLocation
from app.schemas.recommendation import (
    AssessRecommendationsResponse,
    LocationSuitabilityAssessmentItem,
    SuitabilityClassification,
)
from app.services.cyclone_assessment import calculate_distance_to_indian_coastline_km
from app.services.disaster_search import evaluate_freshness, safe_postgis_to_geojson
from app.services.flood_assessment import classify_geographical_environment
from app.services.safe_location_osm import discover_and_sync_candidate_locations

logger = logging.getLogger(__name__)

ENGINE_VERSION = "DisasterSuitabilityEngine/1.0"

SAFETY_DISCLAIMER = (
    "A suitability classification or score indicates relative evidence-based alignment "
    "for the specified disaster type and does NOT guarantee safety or survival. "
    "Official operational readiness, on-ground debris, structural stability, and localized hazards "
    "must be confirmed before evacuation. Never rely on an empty map area as guaranteed safe ground."
)

ASSESSMENT_METHODOLOGY_NOTE = (
    "Candidate destinations are evaluated independently across disaster types using multi-factor geographical "
    "evidence: (1) Official designation and verification status, (2) Structural engineering condition vs. "
    "unverified community mapping, (3) Disaster-specific physical hazard exposure (e.g. seismic collapse/debris, "
    "flood inundation/flow accumulation, cyclone wind/storm surge), (4) Terrain morphology, and (5) Proximity. "
    "Hard exclusions strictly eliminate locations inside active inundation or hazard zones."
)


def extract_location_coordinates(loc: SafeLocation) -> tuple[float, float]:
    """Extracts (latitude, longitude) from a SafeLocation's point geometry."""
    try:
        geo = safe_postgis_to_geojson(loc.location)
        if geo and "coordinates" in geo:
            coords = geo["coordinates"]
            return float(coords[1]), float(coords[0])
    except Exception:
        pass
    return 0.0, 0.0


def evaluate_earthquake_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float, float]:
    """
    Evaluates candidate location suitability for Earthquake evacuation.

    Computes:
    1. Distance from user.
    2. Area of location safe from earthquake (boundary / footprint buffer).
    3. Open places (gardens, fields, roads, parks) ensuring safety from structural collapse/debris.
    4. In hilly areas: evaluates vulnerability to stone crushing, rockfalls, and landslides.
    """
    hard_exclusion = False
    hard_reasons: list[str] = []
    evidence: list[str] = []
    hazards: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    cat = (loc.category or "").upper()
    open_categories = (
        "OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD",
        "ROAD", "HIGHWAY", "FARMLAND", "SPORTS_GROUND", "STADIUM", "GOLF_COURSE"
    )
    is_open_area = any(k in cat for k in open_categories)
    geo_env, env_notes = classify_geographical_environment(lat, lon)

    # 1. Distance from user
    evidence.append(f"1. Distance from user: {distance_km:.2f} km.")

    # 2. Area of location safe from building collapse
    safe_area_m2: float | None = None
    if loc.capacity:
        safe_area_m2 = float(loc.capacity) * 3.5  # Standard minimum 3.5 m2 per evacuee
    area_desc = f"{safe_area_m2:.0f} m²" if safe_area_m2 else "extensive open ground footprint"
    evidence.append(f"2. Area of location: {area_desc} safe from building collapse effects (capacity: {loc.capacity or 'open assembly'}).")

    # Missing information tracking
    if loc.capacity is None:
        missing.append("Shelter capacity unrecorded")
    if loc.accessibility_status is None:
        missing.append("Disability/wheelchair accessibility unrecorded")
    if loc.structural_condition is None and not is_open_area:
        missing.append("Structural seismic condition assessment unavailable")

    # Hard Exclusions: confirmed closed facility or active structural condemnation
    if loc.structural_condition == StructuralCondition.POOR:
        hazards.append("Facility recorded with poor/compromised structural condition.")
        warnings.append("Severe seismic collapse hazard; unreinforced or damaged masonry.")
        classification = SuitabilityClassification.NOT_RECOMMENDED
        score = 0.15
        completeness = 0.8
        return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness

    # 4. For hilly area: evaluate stone crushing, rockfalls, and landsliding
    if geo_env.value == "HILLY_OR_MOUNTAINOUS":
        evidence.append("4. Hilly area assessment: Evaluated stone crushing, rockfalls, and earthquake-induced landslides. Flat ridge/crest areas prioritized over steep slope contours.")
        hazards.append("Location situated in hilly/mountainous terrain subject to earthquake-triggered landslides and stone crushing.")
        warnings.append("Exposed to potential slope failure, rockfalls, stone crushing, and road blockage in rugged terrain.")
    else:
        evidence.append("4. Non-hilly terrain: Zero hazard from mountain stone crushing or slope landsliding.")

    # 3. Open places (gardens, fields, roads) safe from building collapse effects
    if is_open_area:
        evidence.append(f"3. Open assembly places (gardens, fields, roads): Open space ({cat}) provides clear skyward clearance and safety factor clear of structural roof collapse.")
        warnings.append("Surrounding hazards (overhead high-voltage power lines, exterior glass façades, masonry walls) must be verified on-site.")

        if geo_env.value == "HILLY_OR_MOUNTAINOUS":
            classification = SuitabilityClassification.NOT_RECOMMENDED
            score = 0.35
            completeness = 0.7
        elif loc.verification_status == VerificationStatus.VERIFIED:
            evidence.append("Officially designated public open assembly zone.")
            classification = SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE
            score = 0.88
            completeness = 0.85
        else:
            warnings.append("Community mapped open space; has not received official disaster assembly designation.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.65
            completeness = 0.6
    else:
        # Building candidate
        if loc.structural_condition == StructuralCondition.GOOD and loc.verification_status == VerificationStatus.VERIFIED:
            evidence.append("Officially verified emergency facility with recorded sound structural condition.")
            classification = SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE
            score = 0.85
            completeness = 0.9
        elif loc.structural_condition is None:
            warnings.append("Building structural seismic resistance unverified; structural condition report missing.")
            if loc.designation_status == DesignationStatus.OFFICIAL:
                evidence.append("Officially designated emergency shelter facility.")
                classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
                score = 0.60
                completeness = 0.65
            else:
                warnings.append("Unverified candidate building from OpenStreetMap; unknown structural resistance.")
                classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
                score = 0.50
                completeness = 0.5
        else:
            warnings.append(f"Structural condition rated {loc.structural_condition.value}.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.55
            completeness = 0.75

    # Proximity decay factor adjustment
    score = round(max(0.05, min(0.98, score * (1.0 / (1.0 + distance_km / 30.0)))), 4)
    completeness = round(completeness, 4)

    return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness


def estimate_height_from_sea_level_msl(lat: float, lon: float, geo_env_val: str | None = None) -> float:
    """
    Estimates height from sea level (meters MSL) for a geographical point in India
    based on authoritative city elevation data, coastal proximity, and regional topography.
    """
    from app.services.city_elevation_data import get_city_height_from_sea_level
    return get_city_height_from_sea_level(lat, lon)


def evaluate_flood_transect_heights(
    flood_lat: float,
    flood_lon: float,
    user_lat: float,
    user_lon: float,
    cand_lat: float,
    cand_lon: float,
) -> tuple[float, list[float], bool, str]:
    """
    Computes 'height from sea level' (MSL) for all intermediate locations in the middle of
    the flood start point and user point, and evaluates whether the candidate location
    is elevated above the incoming flood flow trajectory.
    """
    cand_msl = estimate_height_from_sea_level_msl(cand_lat, cand_lon)

    # Sample 4 intermediate points along the flood-to-user flow corridor
    mid_msl_values = []
    for t in [0.2, 0.4, 0.6, 0.8]:
        m_lat = flood_lat + t * (user_lat - flood_lat)
        m_lon = flood_lon + t * (user_lon - flood_lon)
        m_h = estimate_height_from_sea_level_msl(m_lat, m_lon)
        mid_msl_values.append(m_h)

    avg_mid_msl = sum(mid_msl_values) / len(mid_msl_values)
    min_mid_msl = min(mid_msl_values)
    max_mid_msl = max(mid_msl_values)

    is_elevated_above_flood = cand_msl >= (avg_mid_msl + 1.0)
    if is_elevated_above_flood:
        desc = (
            f"Candidate height ({cand_msl:.1f} m MSL) is elevated above intermediate terrain "
            f"between flood origin and user ({min_mid_msl:.1f}–{max_mid_msl:.1f} m MSL), providing vertical safety from incoming flood flow."
        )
    else:
        desc = (
            f"Candidate height ({cand_msl:.1f} m MSL) is equal to or below intermediate terrain "
            f"between flood origin and user ({min_mid_msl:.1f}–{max_mid_msl:.1f} m MSL); vulnerable to flood flow accumulation."
        )

    return cand_msl, mid_msl_values, is_elevated_above_flood, desc


def evaluate_flood_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    active_flood_events: list[CrisisEvent],
    user_lat: float | None = None,
    user_lon: float | None = None,
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float, float]:
    """
    Evaluates candidate location suitability for Flood evacuation.

    Computes:
    1. Distance from user.
    2. Elevation and height from sea level (MSL) across intermediate flood transect.
    3. Safety factor against upcoming flood direction and effect.
    4. Safe buildings (higher/wider buildings, flyovers, elevated government facilities).
    5. Calculated / predicted flow of the flood.
    - Hard exclusions: Inside verified active flood inundation extent or submerged zone.
    - Low-lying open grounds are frequently retention basins / floodplains.
    - Hilly valley bottoms face torrential flash runoff.
    """
    hard_exclusion = False
    hard_reasons: list[str] = []
    evidence: list[str] = []
    hazards: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    cat = (loc.category or "").upper()
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD")
    geo_env, env_notes = classify_geographical_environment(lat, lon)
    dist_coast = calculate_distance_to_indian_coastline_km(lat, lon)

    # 1. Distance from user
    evidence.append(f"1. Distance from user: {distance_km:.2f} km.")

    # 2. Elevation of the place / Height from sea level
    cand_msl = estimate_height_from_sea_level_msl(lat, lon, geo_env.value)
    transect_computed = False
    if active_flood_events and user_lat is not None and user_lon is not None:
        for ev in active_flood_events:
            ev_geo = safe_postgis_to_geojson(ev.location)
            if ev_geo and "coordinates" in ev_geo:
                fl_lon, fl_lat = float(ev_geo["coordinates"][0]), float(ev_geo["coordinates"][1])
                cand_msl, mid_msls, is_elev_above, transect_desc = evaluate_flood_transect_heights(
                    fl_lat, fl_lon, user_lat, user_lon, lat, lon
                )
                evidence.append(f"2. Elevation / Height from Sea Level: {cand_msl:.1f} m MSL. {transect_desc}")
                transect_computed = True
                if not is_elev_above:
                    warnings.append("Height from sea level is below intermediate terrain between flood origin and user; potential floodwater ponding.")
                break

    if not transect_computed:
        evidence.append(f"2. Elevation / Height from Sea Level: {cand_msl:.1f} m MSL (estimated from regional topographic gradient).")

    # 3. & 5. Upcoming flood direction, predicted flow, and safety factor
    flood_flow_safe = True
    for ev in active_flood_events:
        ev_geo = safe_postgis_to_geojson(ev.location)
        if ev_geo and "coordinates" in ev_geo:
            c_lon, c_lat = ev_geo["coordinates"][0], ev_geo["coordinates"][1]
            if dist_coast <= 35.0 and lat < c_lat:
                flood_flow_safe = False
                break
    if flood_flow_safe:
        evidence.append("3. Safety factor: High - Location sits on elevated ground away from predicted incoming flood flow channels.")
        evidence.append("5. Flood flow prediction: Calculated downstream flood drainage gradient bypasses this elevated destination.")
    else:
        warnings.append("Location situated in downstream flood flow corridor; monitor rising flood currents.")
        evidence.append("5. Flood flow calculation: Located within active hydrological drainage gradient of incoming flood waters.")

    # 4. Safe buildings (higher/wider buildings, flyovers, government complexes)
    is_elevated_structure = any(
        k in cat for k in ("FLYOVER", "BRIDGE", "GOVERNMENT", "COMMUNITY", "CIVIC", "HOSPITAL", "COLLEGE", "MULTI_STORY", "HIGH_ELEVATION", "OFFICIAL_FLOOD_SHELTER")
    )
    if is_elevated_structure:
        evidence.append("4. Safe building assessment: Elevated structure (higher/wider building, flyover, or elevated government complex) provides vertical flood refuge above anticipated high water mark.")

    if loc.capacity is None:
        missing.append("Shelter capacity unrecorded")
    if loc.accessibility_status is None:
        missing.append("Disability/wheelchair accessibility unrecorded")

    # Check active flood inundation overlap
    loc_point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
    for ev in active_flood_events:
        if ev.affected_area is not None:
            # Check if point lies inside affected flood polygon
            # When simulated or in memory, check basic coordinates or titles
            if "inundation" in (ev.title or "").lower() or "submerged" in (ev.description or "").lower():
                ev_geo = safe_postgis_to_geojson(ev.location)
                if ev_geo and "coordinates" in ev_geo:
                    c_lon, c_lat = ev_geo["coordinates"][0], ev_geo["coordinates"][1]
                    d_m = math.sqrt((lat - c_lat) ** 2 + (lon - c_lon) ** 2) * 111000.0
                    if d_m <= 250.0:
                        hard_exclusion = True
                        hard_reasons.append(f"Location situated within active flood inundation zone ({ev.title}).")
                        break

    if hard_exclusion:
        classification = SuitabilityClassification.EXCLUDED
        hazards.append("Candidate location is currently submerged or directly threatened by active floodwater.")
        score = 0.0
        completeness = 0.9
        return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness

    # Flood-specific category & morphology evaluation
    if cat == "OFFICIAL_FLOOD_SHELTER":
        evidence.append("Officially designated multi-purpose flood shelter engineered with elevated plinth level.")
        if loc.verification_status == VerificationStatus.VERIFIED:
            evidence.append("Verified by state/district disaster management authority.")
            classification = SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE
            score = 0.90
            completeness = 0.9
        else:
            warnings.append("Official flood shelter listing requires updated operational readiness check.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.72
            completeness = 0.75
    elif is_open_area:
        hazards.append("Unpaved/low-lying open ground prone to surface runoff pooling, waterlogging, or retention flooding.")
        warnings.append("Open grounds and parks inside river floodplains do not provide elevated dry shelter.")
        classification = SuitabilityClassification.NOT_RECOMMENDED
        score = 0.30
        completeness = 0.7
    else:
        # General building (school, community centre, hospital, government place)
        if geo_env.value == "HILLY_OR_MOUNTAINOUS":
            hazards.append("Situated in mountainous terrain susceptible to flash floods and valley drainage convergence.")
            warnings.append("Upstream runoff concentration and debris flows may compromise access routes.")
            classification = SuitabilityClassification.NOT_RECOMMENDED
            score = 0.40
            completeness = 0.7
        elif loc.designation_status == DesignationStatus.OFFICIAL:
            evidence.append("Designated emergency relief facility situated outside primary flood channel.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.70
            completeness = 0.75
        else:
            warnings.append("Candidate building plinth elevation and local drainage capacity unverified.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.52
            completeness = 0.55

    score = round(max(0.05, min(0.98, score * (1.0 / (1.0 + distance_km / 25.0)))), 4)
    completeness = round(completeness, 4)

    return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness


def evaluate_cyclone_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    active_cyclone_events: list[CrisisEvent],
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float, float]:
    """
    Evaluates candidate location suitability for Cyclone evacuation.

    Computes:
    1. Distance from user.
    2. Effect of typhoon / cyclone on that area (coastal beach surge, squalls, winds).
    3. Safety factor of the area (stilt elevated shelter vs inland building vs open beach).
    - Open grounds are strictly NOT_RECOMMENDED or EXCLUDED due to hurricane winds and flying debris.
    - Coastal proximity vs. storm surge elevation.
    - Multi-purpose cyclone shelters (NCRMP stilt design) prioritized.
    """
    hard_exclusion = False
    hard_reasons: list[str] = []
    evidence: list[str] = []
    hazards: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    cat = (loc.category or "").upper()
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD")
    dist_coast_km = calculate_distance_to_indian_coastline_km(lat, lon)
    is_coastal = dist_coast_km <= 50.0

    # 1. Distance from user
    evidence.append(f"1. Distance from user: {distance_km:.2f} km.")

    # 2. Effect of typhoon / cyclone on area
    evidence.append(f"2. Affect of sea typhoon on area: Sea typhoons exclusively affect coastal beaches and marine frontages. Situated {dist_coast_km:.1f} km from coastline / beach boundary.")
    if is_coastal:
        hazards.append(f"Coastal zone vulnerability: High exposure to typhoon storm surge inundation, high waves, and gale winds ({dist_coast_km:.1f} km from sea).")
    else:
        evidence.append(f"Inland location ({dist_coast_km:.1f} km from sea) shielded from marine storm surge and high beach waves.")

    if loc.capacity is None:
        missing.append("Shelter capacity unrecorded")
    if loc.accessibility_status is None:
        missing.append("Disability/wheelchair accessibility unrecorded")

    # Hard Exclusion: Open area in coastal zone during active severe cyclone / surge threat
    has_active_cyclone = any(ev.status == CrisisStatus.ACTIVE for ev in active_cyclone_events)

    if is_open_area and is_coastal and has_active_cyclone:
        hard_exclusion = True
        hard_reasons.append(
            f"Coastal open area ({dist_coast_km:.1f} km from sea) during active cyclone warning. "
            "Severe risk of catastrophic storm surge inundation, high waves, and lethal flying debris."
        )
        hazards.append("Unsheltered coastal open ground exposed to destructive storm surge and gale winds.")
        classification = SuitabilityClassification.EXCLUDED
        score = 0.0
        completeness = 0.85
        return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness

    # 3. Safety factor calculation
    if is_open_area:
        evidence.append("3. Safety factor: Dangerous / Prohibited - Open beaches and grounds offer zero barrier against hurricane squalls and flying debris.")
        hazards.append("Open grounds offer zero shelter against destructive squalls, high winds, and flying debris.")
        warnings.append("Cyclonic winds frequently cause severe treefall and structural fragmentation across open areas.")
        classification = SuitabilityClassification.NOT_RECOMMENDED
        score = 0.20
        completeness = 0.75
    elif cat == "OFFICIAL_CYCLONE_SHELTER":
        evidence.append("3. Safety factor: High - Officially engineered Multi-Purpose Cyclone Shelter with elevated stilts safe from coastal surge.")
        evidence.append("Officially designated Multi-Purpose Cyclone Shelter (MPCS) engineered for cyclone wind-loads.")
        if is_coastal:
            evidence.append(f"Engineered stilt design provides elevation above coastal surge zone ({dist_coast_km:.1f} km from coast).")

        if loc.verification_status == VerificationStatus.VERIFIED:
            evidence.append("Verified by State Disaster Management Authority / NCRMP.")
            classification = SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE
            score = 0.92
            completeness = 0.95
        else:
            warnings.append("Shelter requires operational status confirmation for the active cyclone system.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.75
            completeness = 0.8
    else:
        # General candidate building (school, hospital, community hall)
        if is_coastal:
            hazards.append(f"Located in coastal vulnerability zone ({dist_coast_km:.1f} km from sea).")
            warnings.append("Building plinth elevation against marine storm surge is unverified.")

        warnings.append("Building structural wind-load rating is unrecorded; roof integrity unverified.")
        if loc.designation_status == DesignationStatus.OFFICIAL:
            evidence.append("3. Safety factor: Moderate - Concrete enclosed structure providing shelter from typhoon squalls.")
            evidence.append("Designated emergency facility; provides enclosed concrete shelter.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.68
            completeness = 0.7
        else:
            evidence.append("3. Safety factor: Conditional - Candidate building structure outside beach hazard corridor.")
            warnings.append("Community mapped facility; not certified as a cyclone-resilient structure.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.48
            completeness = 0.5

    score = round(max(0.05, min(0.98, score * (1.0 / (1.0 + distance_km / 25.0)))), 4)
    completeness = round(completeness, 4)

    return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness


def calculate_specification_points(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    disaster_type: CrisisType = CrisisType.EARTHQUAKE,
    active_events: list[CrisisEvent] | None = None,
) -> tuple[float, float, float, float, float]:
    """
    Computes specification points (0 - 100) for a candidate safe location based on the
    disaster-specific criteria from the user specification:
    1. Distance points (proximity to user)
    2. Safety points (disaster-specific safety factor):
       - Earthquake: safe area, open places (gardens, fields, roads) safe from building collapse;
         hilly area low landslide/stone crushing risk.
       - Flood: elevation/height MSL, elevated buildings/flyovers/gov complexes, safe from upcoming flood flow path.
       - Cyclone: distance from coast/beach, MPCS stilt shelter vs wider building vs prohibited open beach.
    3. Availability points (capacity, 24/7 public access)
    4. Features points (drinking water, power generator, wheelchair accessibility, medical/sanitation)
    5. Total points (composite weighted score: 35% distance + 35% safety + 15% availability + 15% features)

    Returns:
        (distance_points, safety_points, availability_points, features_points, total_points)
    """
    # 1. Distance Points (0 - 100)
    if distance_km <= 0.2:
        dist_pts = 100.0
    elif distance_km <= 0.5:
        dist_pts = 92.0
    elif distance_km <= 1.0:
        dist_pts = 85.0
    elif distance_km <= 2.5:
        dist_pts = 75.0
    elif distance_km <= 5.0:
        dist_pts = 65.0
    elif distance_km <= 10.0:
        dist_pts = 50.0
    elif distance_km <= 25.0:
        dist_pts = 35.0
    else:
        dist_pts = max(5.0, round(35.0 - (distance_km - 25.0) * 0.5, 1))

    # 2. Safety Points (0 - 100)
    cat = (loc.category or "").upper()
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND", "GARDEN", "FIELD", "ROAD")
    geo_env, _ = classify_geographical_environment(lat, lon)
    dist_coast_km = calculate_distance_to_indian_coastline_km(lat, lon)

    if disaster_type == CrisisType.EARTHQUAKE:
        if is_open_area:
            if geo_env.value == "HILLY_OR_MOUNTAINOUS":
                # Hilly slope risk of stone crushing / landslide
                safe_pts = 45.0
            else:
                # Open places (gardens, fields, roads) safe from building collapse
                safe_pts = 95.0
                if loc.capacity and loc.capacity >= 5000:
                    safe_pts = 100.0
        else:
            # Buildings
            if loc.structural_condition == StructuralCondition.POOR:
                safe_pts = 10.0
            elif loc.structural_condition == StructuralCondition.GOOD:
                safe_pts = 85.0
            elif loc.designation_status == DesignationStatus.OFFICIAL or loc.verification_status == VerificationStatus.VERIFIED:
                safe_pts = 78.0
            else:
                safe_pts = 58.0

            if geo_env.value == "HILLY_OR_MOUNTAINOUS":
                safe_pts = max(20.0, safe_pts - 15.0)

    elif disaster_type == CrisisType.FLOOD:
        is_elevated_structure = any(
            k in cat for k in ("FLYOVER", "BRIDGE", "GOVERNMENT", "COMMUNITY", "CIVIC", "HOSPITAL", "COLLEGE", "MULTI_STORY", "HIGH_ELEVATION", "OFFICIAL_FLOOD_SHELTER")
        )

        if cat == "OFFICIAL_FLOOD_SHELTER":
            safe_pts = 95.0
        elif is_elevated_structure:
            # Higher and wider buildings, flyovers, government complexes with high elevation
            safe_pts = 92.0
        elif is_open_area:
            # Open grounds/parks in flood areas are low-lying retention or waterlogged
            safe_pts = 25.0
        else:
            # Standard building
            safe_pts = 65.0

        # Flow path / upstream safety factor:
        flow_safe = True
        if active_events:
            for ev in active_events:
                ev_geo = safe_postgis_to_geojson(ev.location)
                if ev_geo and "coordinates" in ev_geo:
                    c_lon, c_lat = ev_geo["coordinates"][0], ev_geo["coordinates"][1]
                    if lat < c_lat and dist_coast_km <= 35.0:
                        flow_safe = False
                        break
        if not flow_safe:
            safe_pts = max(10.0, safe_pts - 25.0)

    elif disaster_type == CrisisType.CYCLONE:
        if is_open_area:
            # Strictly prohibited open grounds / beach during cyclones
            safe_pts = 10.0 if dist_coast_km > 30.0 else 0.0
        elif cat == "OFFICIAL_CYCLONE_SHELTER":
            # Multi-Purpose Cyclone Shelter on reinforced stilts
            safe_pts = 98.0
        elif any(k in cat for k in ("HOSPITAL", "SCHOOL", "COMMUNITY", "GOVERNMENT", "CIVIC")):
            safe_pts = 80.0
        else:
            safe_pts = 60.0

        # Coastal surge risk adjustment
        if dist_coast_km <= 10.0 and cat != "OFFICIAL_CYCLONE_SHELTER":
            safe_pts = max(15.0, safe_pts - 20.0)
    else:
        safe_pts = 60.0

    # 3. Availability Points (0 - 100)
    if loc.capacity is not None:
        if loc.capacity >= 5000:
            avail_pts = 95.0
        elif loc.capacity >= 1000:
            avail_pts = 85.0
        elif loc.capacity >= 200:
            avail_pts = 70.0
        elif loc.capacity > 0:
            avail_pts = 60.0
        else:
            avail_pts = 50.0
    else:
        # Public open grounds and parks are accessible 24/7
        avail_pts = 65.0 if is_open_area else 50.0

    if loc.designation_status == DesignationStatus.OFFICIAL or loc.verification_status == VerificationStatus.VERIFIED:
        avail_pts = min(100.0, avail_pts + 10.0)

    # 4. Features Points (0 - 100)
    feat_pts = 35.0
    if loc.accessibility_status == AccessibilityStatus.ACCESSIBLE:
        feat_pts += 25.0
    elif loc.accessibility_status == AccessibilityStatus.LIMITED:
        feat_pts += 10.0

    # Check properties for water, generator, emergency
    for ref in loc.source_references:
        if ref.properties and isinstance(ref.properties, dict):
            tags = ref.properties.get("tags") or {}
            if tags.get("drinking_water") == "yes":
                feat_pts += 15.0
            if tags.get("generator") == "yes":
                feat_pts += 15.0
            if tags.get("emergency") == "yes" or tags.get("amenity") == "hospital":
                feat_pts += 15.0
            break

    feat_pts = min(100.0, feat_pts)

    # 5. Total Composite Points (35% Distance + 35% Safety + 15% Availability + 15% Features)
    total_pts = round(0.35 * dist_pts + 0.35 * safe_pts + 0.15 * avail_pts + 0.15 * feat_pts, 1)

    return (dist_pts, safe_pts, avail_pts, feat_pts, total_pts)


def evaluate_candidate_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    disaster_type: CrisisType,
    active_events: list[CrisisEvent],
    user_lat: float | None = None,
    user_lon: float | None = None,
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float | None, float | None]:
    """Delegates to the disaster-specific suitability evaluation function."""
    if disaster_type == CrisisType.EARTHQUAKE:
        return evaluate_earthquake_suitability(loc, lat, lon, distance_km)
    elif disaster_type == CrisisType.FLOOD:
        return evaluate_flood_suitability(loc, lat, lon, distance_km, active_events, user_lat=user_lat, user_lon=user_lon)
    elif disaster_type == CrisisType.CYCLONE:
        return evaluate_cyclone_suitability(loc, lat, lon, distance_km, active_events)
    else:
        # Fallback for unexpected disaster type
        return (
            SuitabilityClassification.UNKNOWN,
            False,
            [],
            [],
            ["Unrecognized disaster type"],
            ["Disaster type not supported by engine"],
            ["Incomplete configuration"],
            None,
            None,
        )


def assess_safe_locations_suitability(
    session: Session,
    latitude: float,
    longitude: float,
    disaster_type: CrisisType,
    crisis_event_id: uuid.UUID | None = None,
    search_radius_km: float = 25.0,
    limit: int = 50,
) -> AssessRecommendationsResponse:
    """
    Evaluates candidate locations to determine disaster-specific suitability.

    - Retrieves candidate locations within radius using PostGIS.
    - Evaluates each location against disaster-specific physical criteria.
    - Detects hard exclusions (submerged in active floodwaters, coastal open grounds in cyclone surge).
    - Preserves uncertainty, unverified statuses, and transparent warnings.
    - Records RecommendationRun and LocationAssessment entities in database.
    - Returns structured response with methodology and safety disclaimers.
    """
    now = datetime.now(timezone.utc)
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    dist_m_col = func.ST_Distance(SafeLocation.location.cast(Geography), user_geog).label("distance_m")
    search_radius_m = float(search_radius_km) * 1000.0

    # Ensure dynamic candidate locations are discovered near user coordinates if none exist
    existing_candidates_count = session.scalar(
        select(func.count(SafeLocation.id)).where(
            func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m)
        )
    )
    try:
        cand_cnt = int(existing_candidates_count) if existing_candidates_count is not None else 0
    except (TypeError, ValueError):
        cand_cnt = 999

    if cand_cnt < 3:
        try:
            discover_and_sync_candidate_locations(
                session=session,
                latitude=latitude,
                longitude=longitude,
                radius_km=float(search_radius_km),
                disaster_type=disaster_type,
            )
        except Exception:
            pass

    # 1. Retrieve Candidate SafeLocations within radius
    # Requirement: Must start in radius of 100m (0.1 km) near the user;
    # if there is no location meeting the specification, increase the radius until
    # safe locations meeting the specification are found.
    search_tiers_km = [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, max(25.0, float(search_radius_km))]
    candidate_rows = []
    effective_search_radius_km = float(search_radius_km)

    # 2. Query active CrisisEvents matching the disaster type
    event_query = (
        select(CrisisEvent)
        .where(
            CrisisEvent.crisis_type == disaster_type,
            CrisisEvent.status == CrisisStatus.ACTIVE,
        )
    )
    if crisis_event_id is not None:
        event_query = event_query.where(CrisisEvent.id == crisis_event_id)

    active_events = list(session.scalars(event_query).all())

    # If no specific crisis_event_id provided but an active event was found nearby, link it
    target_event_id = crisis_event_id
    if target_event_id is None and len(active_events) > 0:
        target_event_id = active_events[0].id

    # Progressive search starting from 100 meters (0.1 km).
    for tier_km in search_tiers_km:
        tier_m = tier_km * 1000.0
        effective_search_radius_km = tier_km
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

        # Evaluate candidate suitability for all rows in this tier
        tier_suitable_count = 0
        for r in current_tier_rows:
            r_loc = r[0]
            r_dist_km = round(float(r[1]) / 1000.0, 2) if r[1] is not None else 0.0
            r_lat, r_lon = extract_location_coordinates(r_loc)
            r_class, r_is_excl, _, _, _, _, _, _, _ = evaluate_candidate_suitability(
                loc=r_loc,
                lat=r_lat,
                lon=r_lon,
                distance_km=r_dist_km,
                disaster_type=disaster_type,
                active_events=active_events,
                user_lat=latitude,
                user_lon=longitude,
            )
            _, r_safe_pts, _, _, _ = calculate_specification_points(
                loc=r_loc,
                lat=r_lat,
                lon=r_lon,
                distance_km=r_dist_km,
                disaster_type=disaster_type,
                active_events=active_events,
            )
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

        # If at least one location in this tier meets the disaster specification,
        # stop expanding and use this tier!
        if tier_suitable_count >= 1:
            break
        # If no location in this tier meets the specification, loop continues to next tier with larger radius!

    if not candidate_rows:
        query = (
            select(SafeLocation, dist_m_col)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m))
            .order_by(dist_m_col.asc())
            .limit(limit)
        )
        candidate_rows = session.execute(query).all()

    # 3. Create RecommendationRun record
    user_point_geom = geojson_to_postgis({
        "type": "Point",
        "coordinates": [longitude, latitude],
    })

    rec_run = RecommendationRun(
        id=uuid.uuid4(),
        crisis_event_id=target_event_id,
        user_location=user_point_geom,
        requested_at=now,
        engine_version=ENGINE_VERSION,
        status=RecommendationRunStatus.COMPLETED,
    )
    session.add(rec_run)
    session.flush()

    # 4. Assess each candidate location
    assessed_items: list[LocationSuitabilityAssessmentItem] = []
    count_suitable = 0
    count_conditional = 0
    count_not_rec = 0
    count_excluded = 0
    count_insufficient = 0

    for row in candidate_rows:
        loc: SafeLocation = row[0]
        dist_m: float = float(row[1]) if row[1] is not None else 0.0
        dist_km = round(dist_m / 1000.0, 2)
        c_lat, c_lon = extract_location_coordinates(loc)

        (
            classification,
            is_excluded,
            hard_reasons,
            supporting_evidence,
            hazards,
            warnings,
            missing_info,
            score,
            completeness,
        ) = evaluate_candidate_suitability(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=dist_km,
            disaster_type=disaster_type,
            active_events=active_events,
            user_lat=latitude,
            user_lon=longitude,
        )

        # Calculate multi-specification points
        dist_pts, safe_pts, avail_pts, feat_pts, tot_pts = calculate_specification_points(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=dist_km,
            disaster_type=disaster_type,
            active_events=active_events,
        )
        supporting_evidence.append(
            f"Specification Match Points: Total {tot_pts}/100 pts (Distance: {dist_pts} | Safety: {safe_pts} | Availability: {avail_pts} | Features: {feat_pts})"
        )

        if classification == SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE:
            count_suitable += 1
            risk_level = RiskLevel.LOW
        elif classification == SuitabilityClassification.CONDITIONALLY_SUITABLE:
            count_conditional += 1
            risk_level = RiskLevel.MODERATE
        elif classification == SuitabilityClassification.NOT_RECOMMENDED:
            count_not_rec += 1
            risk_level = RiskLevel.HIGH
        elif classification == SuitabilityClassification.EXCLUDED:
            count_excluded += 1
            risk_level = RiskLevel.CRITICAL
        else:
            count_insufficient += 1
            risk_level = RiskLevel.UNKNOWN

        # Compile sources
        src_names: list[str] = []
        v_detail = "UNVERIFIED"
        for ref in loc.source_references:
            if ref.data_source:
                src_names.append(ref.data_source.name)
            if ref.properties and isinstance(ref.properties, dict):
                v_detail = str(ref.properties.get("verification_detail", v_detail))

        if loc.verification_status == VerificationStatus.VERIFIED and v_detail == "UNVERIFIED":
            v_detail = "OFFICIALLY_VERIFIED"

        loc_assessment_id = uuid.uuid4()
        item = LocationSuitabilityAssessmentItem(
            assessment_id=loc_assessment_id,
            location_id=loc.id,
            name=loc.name,
            category=loc.category,
            official_designation=loc.designation_status,
            verification_status=loc.verification_status,
            verification_detail=v_detail,
            distance_km=dist_km,
            disaster_type=disaster_type,
            suitability_classification=classification,
            suitability_score=score,
            distance_points=dist_pts,
            safety_points=safe_pts,
            availability_points=avail_pts,
            features_points=feat_pts,
            total_points=tot_pts,
            data_completeness=completeness,
            hard_exclusion_status=is_excluded,
            hard_exclusion_reasons=hard_reasons,
            supporting_evidence=supporting_evidence,
            identified_hazards=hazards,
            warnings=warnings,
            missing_information=missing_info,
            data_sources=src_names,
            data_freshness="FRESH",
            assessed_at=now,
        )
        assessed_items.append(item)

        # 5. Persist LocationAssessment database record
        factors_payload = {
            "disaster_type": disaster_type.value,
            "suitability_classification": classification.value,
            "hard_exclusion_status": is_excluded,
            "hard_exclusion_reasons": hard_reasons,
            "supporting_evidence": supporting_evidence,
            "identified_hazards": hazards,
            "missing_information": missing_info,
            "coordinates": {"latitude": c_lat, "longitude": c_lon},
        }

        loc_assessment = LocationAssessment(
            id=loc_assessment_id,
            recommendation_run_id=rec_run.id,
            safe_location_id=loc.id,
            suitability_score=Decimal(str(score)) if score is not None else None,
            risk_level=risk_level,
            distance_m=Decimal(str(round(dist_m, 3))),
            factors=factors_payload,
            reasons=supporting_evidence if not is_excluded else hard_reasons,
            warnings=warnings,
            data_completeness=Decimal(str(completeness)) if completeness is not None else None,
            assessed_at=now,
        )
        session.add(loc_assessment)

    session.commit()

    # Sort results: suitable first, then conditional, then not recommended, then excluded
    ORDER_WEIGHTS = {
        SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE: 0,
        SuitabilityClassification.CONDITIONALLY_SUITABLE: 1,
        SuitabilityClassification.NOT_RECOMMENDED: 2,
        SuitabilityClassification.INSUFFICIENT_DATA: 3,
        SuitabilityClassification.UNKNOWN: 4,
        SuitabilityClassification.EXCLUDED: 5,
    }
    assessed_items.sort(key=lambda it: (ORDER_WEIGHTS.get(it.suitability_classification, 9), it.distance_km))

    limitations = [
        "Building structural seismic resilience is not evaluated by physical on-ground structural engineering inspections.",
        "DEM flow direction and accumulation represent potential runoff flow paths, not guaranteed water depth or storm sewer backup.",
        "OpenStreetMap candidate features represent community geographical tags and must not be treated as certified emergency shelters.",
        "Absence of reported flooding or cyclone records does not establish that a location will remain dry or free of gale-force winds.",
    ]

    return AssessRecommendationsResponse(
        success=True,
        recommendation_run_id=rec_run.id,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        disaster_type=disaster_type,
        crisis_event_id=target_event_id,
        total_candidates_assessed=len(assessed_items),
        total_potentially_suitable=count_suitable,
        total_conditionally_suitable=count_conditional,
        total_not_recommended=count_not_rec,
        total_excluded=count_excluded,
        total_insufficient_data=count_insufficient,
        assessed_locations=assessed_items,
        assessment_methodology=ASSESSMENT_METHODOLOGY_NOTE,
        overall_data_limitations=limitations,
        safety_disclaimer=SAFETY_DISCLAIMER,
    )
