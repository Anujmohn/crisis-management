from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from geoalchemy2 import Geography
from geoalchemy2.elements import WKBElement, WKTElement
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database.geometry import postgis_to_geojson
from app.models.core import CrisisEvent, CrisisSourceReport, HazardObservation
from app.models.enums import CrisisStatus, CrisisType
from app.schemas.disaster_search import (
    DisasterEventSummary,
    DisasterObservationSummary,
    DisasterSourceSummary,
    NearbyDisastersResponse,
    UserLocation,
)

logger = logging.getLogger(__name__)


def safe_postgis_to_geojson(geom: Any) -> dict[str, Any] | None:
    """
    Safely converts a PostGIS geometry (WKBElement or WKTElement) or dict into a GeoJSON mapping.
    """
    if geom is None:
        return None
    if isinstance(geom, dict):
        coords = geom.get("coordinates")
        if coords is not None and len(coords) == 0:
            return None
        return geom
    if isinstance(geom, (WKBElement, WKTElement)):
        try:
            return postgis_to_geojson(geom)
        except Exception as exc:
            logger.debug(f"Failed to convert PostGIS geometry to GeoJSON: {exc}")
            return None
    return None


def evaluate_freshness(event: CrisisEvent, now: datetime) -> str:
    """
    Determines disaster event data freshness based on validity periods and update timestamps.
    Returns: 'FRESH' | 'STALE' | 'EXPIRED' | 'UNKNOWN'
    """
    if event.status == CrisisStatus.EXPIRED:
        return "EXPIRED"

    if event.expires_at is not None:
        if event.expires_at < now:
            return "EXPIRED"
        return "FRESH"

    # Evaluate reports validity
    has_future_validity = False
    has_expired_validity = False
    for report in event.source_reports:
        if report.valid_until is not None:
            if report.valid_until < now:
                has_expired_validity = True
            else:
                has_future_validity = True

    if has_future_validity:
        return "FRESH"
    if has_expired_validity and not has_future_validity:
        return "EXPIRED"

    # Check update recency against now
    ref_time = event.last_seen_at or event.first_seen_at
    if ref_time is not None:
        # Ensure aware datetime
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)
        age = now - ref_time
        if age <= timedelta(hours=6):
            return "FRESH"
        elif age <= timedelta(hours=48):
            return "STALE"
        else:
            return "STALE"

    return "UNKNOWN"


def generate_disaster_flow_path(event: CrisisEvent) -> dict[str, Any] | None:
    """
    Generates the directional flow path for active flood warning areas or
    cyclone storm tracks across the affected hazard corridor.
    """
    loc_geo = safe_postgis_to_geojson(event.location)
    coords = None
    if loc_geo and "coordinates" in loc_geo:
        coords = loc_geo["coordinates"]
    elif event.affected_area is not None:
        area_geo = safe_postgis_to_geojson(event.affected_area)
        if area_geo and "coordinates" in area_geo:
            c = area_geo["coordinates"]
            if area_geo.get("type") == "MultiPolygon" and c and c[0] and c[0][0]:
                coords = c[0][0][0]
            elif c and c[0]:
                coords = c[0][0]

    if not coords or len(coords) < 2:
        return None

    lon, lat = float(coords[0]), float(coords[1])

    if event.crisis_type == CrisisType.FLOOD:
        # Downstream drainage vector in India
        if lat > 24.0 and lon >= 88.0:
            # Brahmaputra basin (flows W-SW then S)
            dx, dy = -0.06, -0.05
        elif lat > 24.0:
            # Ganges-Yamuna basin (flows E-SE)
            dx, dy = 0.08, -0.035
        elif lon < 75.0:
            # West-flowing rivers (Narmada / Tapi into Arabian Sea)
            dx, dy = -0.07, -0.02
        else:
            # Peninsular East-flowing rivers (Mahanadi, Godavari, Krishna, Cauvery into Bay of Bengal)
            dx, dy = 0.08, -0.03

        pts = []
        curr_x, curr_y = lon - (dx * 1.5), lat - (dy * 1.5)
        for i in range(5):
            meander = 0.015 if i % 2 == 1 else -0.015
            pts.append([round(curr_x, 5), round(curr_y + (meander if abs(dx) > abs(dy) else 0), 5)])
            curr_x += dx
            curr_y += dy

        return {"type": "LineString", "coordinates": pts}

    elif event.crisis_type == CrisisType.CYCLONE:
        # Cyclone landfall storm track corridor (SE to NW curved inland)
        pts = [
            [round(lon + 0.35, 5), round(lat - 0.25, 5)],
            [round(lon + 0.15, 5), round(lat - 0.10, 5)],
            [round(lon, 5), round(lat, 5)],
            [round(lon - 0.18, 5), round(lat + 0.12, 5)],
            [round(lon - 0.32, 5), round(lat + 0.28, 5)],
        ]
        return {"type": "LineString", "coordinates": pts}

    return None


def get_nearby_disasters(
    session: Session,
    latitude: float,
    longitude: float,
    radius_km: float = 50.0,
    disaster_types: list[CrisisType] | None = None,
    statuses: list[CrisisStatus] | None = None,
    limit: int = 50,
) -> NearbyDisastersResponse:
    """
    Queries nearby disaster events using PostGIS geospatial distance calculations.

    - Computes metric geodesic distance using PostGIS Geography.
    - Matches events if point location is within radius OR affected_area polygon intersects/covers radius.
    - Supports events with point only, polygon only, or both.
    - Filters by disaster category and status when requested.
    - Eagerly loads sources and observations to prevent N+1 database queries.
    - Normalizes into a clean GeoJSON-compatible response with freshness and provenance.
    """
    now = datetime.now(timezone.utc)
    radius_meters = float(radius_km) * 1000.0

    # User coordinate as EPSG:4326 Geography
    user_geog = func.cast(
        func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326),
        Geography,
    )

    # Distances to location point and affected_area polygon
    loc_dist = func.ST_Distance(func.cast(CrisisEvent.location, Geography), user_geog)
    area_dist = func.ST_Distance(func.cast(CrisisEvent.affected_area, Geography), user_geog)

    # Shortest distance to either point or polygon boundary
    distance_expr = case(
        (
            and_(CrisisEvent.location.is_not(None), CrisisEvent.affected_area.is_not(None)),
            func.least(loc_dist, area_dist),
        ),
        (
            CrisisEvent.location.is_not(None),
            loc_dist,
        ),
        else_=area_dist,
    )

    # Spatial filter: matches if either geometry is within radius_meters
    spatial_filter = or_(
        and_(
            CrisisEvent.location.is_not(None),
            func.ST_DWithin(
                func.cast(CrisisEvent.location, Geography),
                user_geog,
                radius_meters,
            ),
        ),
        and_(
            CrisisEvent.affected_area.is_not(None),
            func.ST_DWithin(
                func.cast(CrisisEvent.affected_area, Geography),
                user_geog,
                radius_meters,
            ),
        ),
    )

    stmt = (
        select(CrisisEvent, distance_expr.label("distance_meters"))
        .options(
            selectinload(CrisisEvent.source_reports).joinedload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
        .where(spatial_filter)
    )

    if disaster_types:
        stmt = stmt.where(CrisisEvent.crisis_type.in_(disaster_types))

    if statuses:
        stmt = stmt.where(CrisisEvent.status.in_(statuses))

    stmt = stmt.order_by(distance_expr.asc()).limit(limit)

    results = session.execute(stmt).all()

    events: list[DisasterEventSummary] = []
    for row in results:
        event = row[0]
        distance_meters = row[1] if len(row) > 1 else None
        distance_km = round(float(distance_meters) / 1000.0, 2) if distance_meters is not None else None

        freshness = evaluate_freshness(event, now)

        # Sources provenance
        sources = [
            DisasterSourceSummary(
                source_name=sr.data_source.name if sr.data_source else "Unknown Official Source",
                source_type=sr.data_source.source_type if sr.data_source else "UNKNOWN",
                source_url=sr.source_url,
                external_event_id=sr.external_event_id,
                issued_at=sr.issued_at,
                fetched_at=sr.fetched_at,
                valid_until=sr.valid_until,
            )
            for sr in (event.source_reports or [])
        ]

        # Hazard observations
        observations = [
            DisasterObservationSummary(
                id=obs.id,
                observation_type=obs.observation_type,
                value_numeric=obs.value_numeric,
                value_text=obs.value_text,
                unit=obs.unit,
                geometry=safe_postgis_to_geojson(obs.geometry),
                observed_at=obs.observed_at,
                properties=obs.properties,
            )
            for obs in (event.hazard_observations or [])
        ]

        events.append(
            DisasterEventSummary(
                id=event.id,
                disaster_type=event.crisis_type,
                title=event.title,
                description=event.description,
                severity=event.severity,
                status=event.status,
                location=safe_postgis_to_geojson(event.location),
                affected_area=safe_postgis_to_geojson(event.affected_area),
                flow_path=generate_disaster_flow_path(event),
                distance_km=distance_km,
                started_at=event.started_at,
                expires_at=event.expires_at,
                first_seen_at=event.first_seen_at,
                last_seen_at=event.last_seen_at,
                freshness=freshness,
                sources=sources,
                observations=observations,
            )
        )

    return NearbyDisastersResponse(
        success=True,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        search_radius_km=radius_km,
        total_events=len(events),
        events=events,
    )


def get_all_active_disasters(
    session: Session,
    disaster_types: list[CrisisType] | None = None,
    statuses: list[CrisisStatus] | None = None,
    limit: int = 200,
) -> NearbyDisastersResponse:
    """
    Retrieves all current active disaster events occurring across India without requiring user proximity.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(CrisisEvent)
        .options(
            selectinload(CrisisEvent.source_reports).joinedload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
    )

    if disaster_types:
        stmt = stmt.where(CrisisEvent.crisis_type.in_(disaster_types))

    if statuses:
        stmt = stmt.where(CrisisEvent.status.in_(statuses))
    else:
        # Include ACTIVE and UNKNOWN (ongoing) by default
        stmt = stmt.where(CrisisEvent.status.in_([CrisisStatus.ACTIVE, CrisisStatus.UNKNOWN]))

    stmt = stmt.order_by(CrisisEvent.last_seen_at.desc(), CrisisEvent.first_seen_at.desc()).limit(limit)
    results = session.scalars(stmt).all()

    events: list[DisasterEventSummary] = []
    for event in results:
        freshness = evaluate_freshness(event, now)

        sources = [
            DisasterSourceSummary(
                source_name=sr.data_source.name if sr.data_source else "Unknown Official Source",
                source_type=sr.data_source.source_type if sr.data_source else "UNKNOWN",
                source_url=sr.source_url,
                external_event_id=sr.external_event_id,
                issued_at=sr.issued_at,
                fetched_at=sr.fetched_at,
                valid_until=sr.valid_until,
            )
            for sr in (event.source_reports or [])
        ]

        observations = [
            DisasterObservationSummary(
                id=obs.id,
                observation_type=obs.observation_type,
                value_numeric=obs.value_numeric,
                value_text=obs.value_text,
                unit=obs.unit,
                geometry=safe_postgis_to_geojson(obs.geometry),
                observed_at=obs.observed_at,
                properties=obs.properties,
            )
            for obs in (event.hazard_observations or [])
        ]

        events.append(
            DisasterEventSummary(
                id=event.id,
                disaster_type=event.crisis_type,
                title=event.title,
                description=event.description,
                severity=event.severity,
                status=event.status,
                location=safe_postgis_to_geojson(event.location),
                affected_area=safe_postgis_to_geojson(event.affected_area),
                flow_path=generate_disaster_flow_path(event),
                distance_km=None,
                started_at=event.started_at,
                expires_at=event.expires_at,
                first_seen_at=event.first_seen_at,
                last_seen_at=event.last_seen_at,
                freshness=freshness,
                sources=sources,
                observations=observations,
            )
        )

    return NearbyDisastersResponse(
        success=True,
        user_location=UserLocation(latitude=20.5937, longitude=78.9629),
        search_radius_km=3500.0,
        total_events=len(events),
        events=events,
    )

