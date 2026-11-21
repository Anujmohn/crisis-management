from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from app.database.geometry import postgis_to_geojson
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation
from app.schemas.earthquake_assessment import (
    AssessedEarthquakeEvent,
    AssessmentStatus,
    DepthClassification,
    EarthquakeAssessmentResponse,
    EarthquakeAssessmentSearchParameters,
)
from app.services.disaster_search import evaluate_freshness, safe_postgis_to_geojson

logger = logging.getLogger(__name__)

SHALLOW_DEPTH_KM = 70.0
INTERMEDIATE_DEPTH_KM = 300.0

EARTHQUAKE_SCIENTIFIC_LIMITATIONS = [
    (
        "This assessment analyzes past recorded earthquake events from official seismological feeds (NCS). "
        "It is NOT an earthquake prediction system. Science currently cannot predict the time, location, "
        "or magnitude of future earthquakes or aftershocks."
    ),
    (
        "This assessment evaluates geographical proximity and available source parameters only. "
        "It does NOT evaluate building structural integrity, site-specific soil liquefaction, "
        "or ground failure susceptibility."
    ),
    (
        "Absence of recorded earthquakes within the search window does not imply that a geographical "
        "location is immune from seismic hazard or future seismic activity."
    ),
    (
        "The system does not recommend specific safe buildings, shelters, or evacuation routes."
    ),
]


def calculate_haversine_distance_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """
    Computes great-circle distance between two pairs of WGS84 coordinates in kilometers.
    """
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_km * c


def classify_focal_depth(depth_km: float | None) -> tuple[str, str]:
    """
    Classifies focal depth according to standard seismological criteria:
    - Shallow: < 70 km (higher surface shaking potential)
    - Intermediate: 70 - 300 km (broader regional felt area, attenuated surface accelerations)
    - Deep: > 300 km (substantial crustal/mantle attenuation)
    - Unknown: when depth is unrecorded
    """
    if depth_km is None:
        return (
            DepthClassification.UNKNOWN.value,
            "Focal depth is not recorded in the available official data.",
        )
    if depth_km < SHALLOW_DEPTH_KM:
        return (
            DepthClassification.SHALLOW.value,
            "Shallow focal depth (< 70 km): seismic energy undergoes less attenuation, "
            "producing higher localized ground shaking near the epicenter.",
        )
    if depth_km <= INTERMEDIATE_DEPTH_KM:
        return (
            DepthClassification.INTERMEDIATE.value,
            "Intermediate focal depth (70–300 km): seismic waves propagate over a broader regional area "
            "with moderated peak ground acceleration near the surface.",
        )
    return (
        DepthClassification.DEEP.value,
        "Deep focal depth (> 300 km): substantial energy attenuation occurs before seismic waves reach surface ground.",
    )


def extract_event_coordinates(event: CrisisEvent) -> tuple[float | None, float | None]:
    """
    Extracts (latitude, longitude) from event geometry or associated reports.
    """
    if event.location is not None:
        geojson = safe_postgis_to_geojson(event.location)
        if geojson and geojson.get("type") == "Point":
            coords = geojson.get("coordinates")
            if coords and len(coords) >= 2:
                return float(coords[1]), float(coords[0])

    for report in getattr(event, "source_reports", []):
        if report.raw_payload and isinstance(report.raw_payload, dict):
            lat = report.raw_payload.get("latitude")
            lon = report.raw_payload.get("longitude")
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon)
                except (ValueError, TypeError):
                    pass

    return None, None


def extract_earthquake_metrics(
    event: CrisisEvent,
) -> tuple[float | None, float | None, str | None, str | None, str | None]:
    """
    Extracts magnitude, depth_km, location_name, reported_intensity, and reported_impact
    from HazardObservations, CrisisSourceReports, and event metadata.
    """
    magnitude: float | None = None
    depth_km: float | None = None
    location_name: str | None = None
    reported_intensity: str | None = None
    reported_impact: str | None = None

    # 1. Search HazardObservations
    for obs in getattr(event, "hazard_observations", []):
        obs_type = (obs.observation_type or "").upper()
        if "MAGNITUDE" in obs_type and magnitude is None:
            if obs.value_numeric is not None:
                magnitude = float(obs.value_numeric)
        elif "DEPTH" in obs_type and depth_km is None:
            if obs.value_numeric is not None:
                depth_km = float(obs.value_numeric)
        elif "INTENSITY" in obs_type and reported_intensity is None:
            reported_intensity = obs.value_text or (str(obs.value_numeric) if obs.value_numeric else None)
        elif "DAMAGE" in obs_type or "IMPACT" in obs_type:
            reported_impact = obs.value_text

        if obs.properties and isinstance(obs.properties, dict):
            if depth_km is None and "depth_km" in obs.properties:
                try:
                    depth_km = float(obs.properties["depth_km"])
                except (ValueError, TypeError):
                    pass
            if magnitude is None and "magnitude" in obs.properties:
                try:
                    magnitude = float(obs.properties["magnitude"])
                except (ValueError, TypeError):
                    pass

    # 2. Search CrisisSourceReports
    for report in getattr(event, "source_reports", []):
        if report.raw_payload and isinstance(report.raw_payload, dict):
            payload = report.raw_payload
            if magnitude is None and "magnitude" in payload and payload["magnitude"] is not None:
                try:
                    magnitude = float(payload["magnitude"])
                except (ValueError, TypeError):
                    pass
            if depth_km is None and "depth_km" in payload and payload["depth_km"] is not None:
                try:
                    depth_km = float(payload["depth_km"])
                except (ValueError, TypeError):
                    pass
            if location_name is None:
                location_name = payload.get("location") or payload.get("region")

            if reported_intensity is None and "intensity" in payload:
                reported_intensity = str(payload["intensity"])
            if reported_impact is None and "impact" in payload:
                reported_impact = str(payload["impact"])

        if magnitude is None and report.source_severity:
            try:
                magnitude = float(report.source_severity)
            except (ValueError, TypeError):
                pass

        if location_name is None and report.source_description:
            location_name = report.source_description

    # 3. Fallback extraction from Title / Description
    if magnitude is None and event.title:
        match = re.search(r"M\s*([0-9]+(?:\.[0-9]+)?)", event.title, re.IGNORECASE)
        if match:
            try:
                magnitude = float(match.group(1))
            except ValueError:
                pass

    if depth_km is None and event.description:
        match = re.search(
            r"depth\s*(?:of)?\s*([0-9]+(?:\.[0-9]+)?)\s*km",
            event.description,
            re.IGNORECASE,
        )
        if match:
            try:
                depth_km = float(match.group(1))
            except ValueError:
                pass

    return magnitude, depth_km, location_name, reported_intensity, reported_impact


def assess_earthquake_hazard(
    session: Session,
    latitude: float,
    longitude: float,
    lookback_days: int = 30,
    search_radius_km: float = 300.0,
) -> EarthquakeAssessmentResponse:
    """
    Evaluates recorded earthquake hazard around a specific geographical coordinate.
    Queries PostGIS for historical/recent earthquakes within lookback_days and search_radius_km.
    Computes epicentral and hypocentral distances, classifies focal depths, and
    evaluates data completeness and freshness.
    """
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(days=lookback_days)
    radius_meters = search_radius_km * 1000.0

    missing_data_warnings: list[str] = []

    user_location = UserLocation(latitude=latitude, longitude=longitude)
    search_params = EarthquakeAssessmentSearchParameters(
        lookback_days=lookback_days,
        search_radius_km=search_radius_km,
    )

    # PostGIS Point geography for user location
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    distance_expr = func.ST_Distance(CrisisEvent.location.cast(Geography), user_geog)

    # Query matching earthquake events
    stmt = (
        select(CrisisEvent, distance_expr.label("distance_meters"))
        .where(
            and_(
                CrisisEvent.crisis_type == CrisisType.EARTHQUAKE,
                CrisisEvent.location.is_not(None),
                CrisisEvent.started_at.is_not(None),
                CrisisEvent.started_at >= cutoff_time,
                func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, radius_meters),
            )
        )
        .options(
            selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
        .order_by(distance_expr.asc(), CrisisEvent.started_at.desc())
    )

    try:
        results = session.execute(stmt).all()
    except Exception as exc:
        logger.warning(
            "Spatial query failed via database engine; falling back to memory/mock evaluation: %s",
            exc,
        )
        # Fallback for unit testing with mocked sessions
        stmt_fallback = (
            select(CrisisEvent)
            .where(
                and_(
                    CrisisEvent.crisis_type == CrisisType.EARTHQUAKE,
                    CrisisEvent.started_at.is_not(None),
                    CrisisEvent.started_at >= cutoff_time,
                )
            )
            .options(
                selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
                selectinload(CrisisEvent.hazard_observations),
            )
        )
        raw_events = session.scalars(stmt_fallback).all()
        results = []
        for ev in raw_events:
            ev_lat, ev_lon = extract_event_coordinates(ev)
            if ev_lat is not None and ev_lon is not None:
                d_km = calculate_haversine_distance_km(latitude, longitude, ev_lat, ev_lon)
                if d_km <= search_radius_km:
                    results.append((ev, d_km * 1000.0))
        results.sort(key=lambda item: item[1])

    events_analyzed_count = len(results)

    # Check for empty results
    if events_analyzed_count == 0:
        summary_msg = (
            f"No relevant earthquake event was found in the available dataset within "
            f"{search_radius_km:.1f} km over the past {lookback_days} days. "
            f"This does not establish that the location is safe."
        )
        return EarthquakeAssessmentResponse(
            success=True,
            user_location=user_location,
            assessment_timestamp=now,
            assessment_status=AssessmentStatus.NO_RELEVANT_EVENT_FOUND.value,
            summary_message=summary_msg,
            search_parameters=search_params,
            events_analyzed_count=0,
            relevant_events=[],
            missing_data_warnings=[],
            scientific_limitations=EARTHQUAKE_SCIENTIFIC_LIMITATIONS,
        )

    assessed_events: list[AssessedEarthquakeEvent] = []
    all_stale = True

    for event, dist_meters in results:
        ev_lat, ev_lon = extract_event_coordinates(event)
        if ev_lat is None or ev_lon is None:
            # Missing coordinates
            missing_data_warnings.append(
                f"Event {event.id} is missing valid coordinates and could not be mapped precisely."
            )

        epicentral_distance_km = (
            round(float(dist_meters) / 1000.0, 2)
            if dist_meters is not None
            else (
                round(calculate_haversine_distance_km(latitude, longitude, ev_lat, ev_lon), 2)
                if ev_lat is not None and ev_lon is not None
                else 0.0
            )
        )

        magnitude, depth_km, loc_name, intensity, impact = extract_earthquake_metrics(event)

        # Depth classification & hypocentral distance
        depth_class, depth_note = classify_focal_depth(depth_km)
        hypocentral_distance_km: float | None = None
        if depth_km is not None:
            hypocentral_distance_km = round(
                math.sqrt(epicentral_distance_km**2 + depth_km**2),
                2,
            )

        event_notes = [depth_note]

        # Completeness evaluation
        if magnitude is not None and depth_km is not None:
            completeness = "COMPLETE"
        elif magnitude is not None or depth_km is not None:
            completeness = "PARTIAL"
            if depth_km is None:
                missing_data_warnings.append(
                    f"Event '{event.title or str(event.id)}': focal depth missing; hypocentral distance unavailable."
                )
            if magnitude is None:
                missing_data_warnings.append(
                    f"Event '{event.title or str(event.id)}': magnitude unrecorded in source data."
                )
        else:
            completeness = "MINIMAL"
            missing_data_warnings.append(
                f"Event '{event.title or str(event.id)}': both magnitude and focal depth are missing."
            )

        # Intensity/damage notes
        if intensity is None:
            event_notes.append("No official localized intensity observations reported for this event.")
        if impact is None:
            event_notes.append("No structural or ground impact observations recorded in official bulletins.")

        # Freshness evaluation
        freshness = evaluate_freshness(event, now)
        if freshness not in ("STALE", "EXPIRED"):
            all_stale = False

        # Build source summaries
        sources_summary: list[DisasterSourceSummary] = []
        for report in getattr(event, "source_reports", []):
            src_name = report.data_source.name if report.data_source else "Unknown Source"
            src_type = report.data_source.source_type if report.data_source else "UNKNOWN"
            sources_summary.append(
                DisasterSourceSummary(
                    source_name=src_name,
                    source_type=src_type,
                    source_url=report.source_url,
                    external_event_id=report.external_event_id,
                    issued_at=report.issued_at,
                    fetched_at=report.fetched_at or now,
                    valid_until=report.valid_until,
                )
            )

        assessed_events.append(
            AssessedEarthquakeEvent(
                id=event.id,
                title=event.title,
                description=event.description,
                origin_time=event.started_at,
                magnitude=magnitude,
                magnitude_type="M",
                depth_km=depth_km,
                depth_classification=depth_class,
                epicentral_distance_km=epicentral_distance_km,
                hypocentral_distance_km=hypocentral_distance_km,
                latitude=ev_lat,
                longitude=ev_lon,
                location_name=loc_name,
                reported_intensity=intensity,
                reported_impact=impact,
                severity=event.severity,
                status=event.status,
                freshness=freshness,
                sources=sources_summary,
                data_completeness=completeness,
                event_notes=event_notes,
            )
        )

    # Determine status
    if all_stale:
        assessment_status = AssessmentStatus.DATA_STALE.value
        summary_msg = (
            f"Found {len(assessed_events)} earthquake event(s) within {search_radius_km:.1f} km "
            f"over the past {lookback_days} days, but all records are marked stale or expired."
        )
    else:
        assessment_status = AssessmentStatus.POTENTIAL_RELEVANT_EVENT.value
        closest = assessed_events[0]
        summary_msg = (
            f"Found {len(assessed_events)} relevant earthquake event(s) within {search_radius_km:.1f} km "
            f"over the past {lookback_days} days. Closest recorded epicenter is {closest.epicentral_distance_km:.1f} km away "
            f"({closest.title or 'Earthquake'})."
        )

    return EarthquakeAssessmentResponse(
        success=True,
        user_location=user_location,
        assessment_timestamp=now,
        assessment_status=assessment_status,
        summary_message=summary_msg,
        search_parameters=search_params,
        events_analyzed_count=events_analyzed_count,
        relevant_events=assessed_events,
        missing_data_warnings=missing_data_warnings,
        scientific_limitations=EARTHQUAKE_SCIENTIFIC_LIMITATIONS,
    )
