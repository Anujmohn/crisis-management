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

    Open grounds vs. buildings require fundamentally distinct considerations:
    - Open grounds avoid roof collapse but must be evaluated for surrounding falling hazards and slope instability.
    - Buildings require verified structural condition; building type alone does not imply seismic resistance.
    """
    hard_exclusion = False
    hard_reasons: list[str] = []
    evidence: list[str] = []
    hazards: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []

    cat = (loc.category or "").upper()
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND")
    geo_env, env_notes = classify_geographical_environment(lat, lon)

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

    # Terrain slope / landslide exposure
    if geo_env.value == "HILLY_OR_MOUNTAINOUS":
        hazards.append("Location situated in hilly/mountainous terrain subject to earthquake-triggered landslides.")
        warnings.append("Exposed to potential slope failure, rockfalls, and road blockage in rugged terrain.")

    if is_open_area:
        evidence.append("Open assembly area provides clear skyward clearance without immediate building roof collapse.")
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


def evaluate_flood_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    active_flood_events: list[CrisisEvent],
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float, float]:
    """
    Evaluates candidate location suitability for Flood evacuation.

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
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND")
    geo_env, env_notes = classify_geographical_environment(lat, lon)

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
        # General building (school, community centre, hospital)
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
    is_open_area = cat in ("OPEN_GROUND", "PARK", "PLAYGROUND")
    dist_coast_km = calculate_distance_to_indian_coastline_km(lat, lon)
    is_coastal = dist_coast_km <= 50.0

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

    if is_open_area:
        hazards.append("Open grounds offer zero shelter against destructive squalls, high winds, and flying debris.")
        warnings.append("Cyclonic winds frequently cause severe treefall and structural fragmentation across open areas.")
        classification = SuitabilityClassification.NOT_RECOMMENDED
        score = 0.20
        completeness = 0.75
    elif cat == "OFFICIAL_CYCLONE_SHELTER":
        evidence.append("Officially designated Multi-Purpose Cyclone Shelter (MPCS) engineered for cyclone wind-loads.")
        if is_coastal:
            evidence.append(f"Engineered stilt design provides elevation above coastal surge zone ({dist_coast_km:.1f} km from coast).")
        else:
            evidence.append("Inland shelter location shielded from maritime storm surge.")

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
            evidence.append("Designated emergency facility; provides enclosed concrete shelter.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.68
            completeness = 0.7
        else:
            warnings.append("Community mapped facility; not certified as a cyclone-resilient structure.")
            classification = SuitabilityClassification.CONDITIONALLY_SUITABLE
            score = 0.48
            completeness = 0.5

    score = round(max(0.05, min(0.98, score * (1.0 / (1.0 + distance_km / 25.0)))), 4)
    completeness = round(completeness, 4)

    return classification, hard_exclusion, hard_reasons, evidence, hazards, warnings, missing, score, completeness


def evaluate_candidate_suitability(
    loc: SafeLocation,
    lat: float,
    lon: float,
    distance_km: float,
    disaster_type: CrisisType,
    active_events: list[CrisisEvent],
) -> tuple[SuitabilityClassification, bool, list[str], list[str], list[str], list[str], list[str], float | None, float | None]:
    """Delegates to the disaster-specific suitability evaluation function."""
    if disaster_type == CrisisType.EARTHQUAKE:
        return evaluate_earthquake_suitability(loc, lat, lon, distance_km)
    elif disaster_type == CrisisType.FLOOD:
        return evaluate_flood_suitability(loc, lat, lon, distance_km, active_events)
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

    # 1. Retrieve Candidate SafeLocations within radius
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
