from __future__ import annotations

import logging
import uuid
from typing import Any

from geoalchemy2 import Geography
from shapely.geometry import shape
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import DesignationStatus, VerificationStatus
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.schemas.disaster_search import UserLocation
from app.schemas.safe_location import (
    NearbySafeLocationsResponse,
    SafeLocationDetailResponse,
    SafeLocationItem,
    SafeLocationSourceSummary,
)
from app.services.disaster_search import safe_postgis_to_geojson
from app.services.recommendation_engine import (
    calculate_specification_points,
    extract_location_coordinates,
)
from app.services.safe_location_osm import discover_and_sync_candidate_locations

logger = logging.getLogger(__name__)

SAFE_LOCATION_DATA_INTEGRITY_NOTICE = (
    "OpenStreetMap and candidate public facilities represent geographical features, "
    "not proof of disaster safety or official shelter designation. "
    "Only facilities marked OFFICIALLY_VERIFIED are confirmed emergency relief destinations "
    "designated by government disaster management authorities. "
    "Candidate open areas and unverified buildings must not be treated as safe evacuation destinations."
)

DISASTER_SUITABILITY_DISCLAIMER = (
    "Identification of a location as a potential shelter or open space does not guarantee safety "
    "across all disaster types. During earthquakes, open areas require surrounding hazard analysis "
    "(debris, glass, powerlines), while buildings require seismic structural verification. "
    "During floods, low-lying open grounds may be submerged. "
    "During cyclones, coastal shelters require storm-surge elevation and wind-load engineering. "
    "Detailed disaster-specific suitability evaluation is required before evacuation routing."
)


def extract_missing_information(loc: SafeLocation) -> list[str]:
    """Generates transparent notifications of unrecorded or unverified attributes."""
    missing: list[str] = []
    if loc.capacity is None:
        missing.append("Official shelter capacity is unrecorded or not published.")
    if loc.structural_condition is None:
        missing.append("Structural engineering condition assessment report is unavailable.")
    if loc.accessibility_status is None:
        missing.append("Disability / wheelchair physical accessibility status is unrecorded.")
    if loc.verification_status != VerificationStatus.VERIFIED:
        missing.append(
            "Location is an unverified candidate facility; not officially designated "
            "by government disaster management authorities."
        )
    return missing


def format_safe_location_item(
    loc: SafeLocation,
    distance_km: float | None = None,
) -> SafeLocationItem:
    """Formats a database SafeLocation record into a schema item with source attribution."""
    sources: list[SafeLocationSourceSummary] = []
    v_detail = "UNVERIFIED"

    for ref in loc.source_references:
        d_name = ref.data_source.name if ref.data_source else "Unknown Source"
        d_code = ref.data_source.code if ref.data_source else "UNKNOWN"
        detail = None
        if ref.properties and isinstance(ref.properties, dict):
            detail = ref.properties.get("verification_detail")
            if detail and v_detail == "UNVERIFIED":
                v_detail = str(detail)

        sources.append(
            SafeLocationSourceSummary(
                data_source_id=ref.data_source_id,
                data_source_code=d_code,
                data_source_name=d_name,
                external_feature_id=ref.external_feature_id,
                source_url=ref.source_url,
                last_seen_at=ref.last_seen_at,
                verification_detail=detail,
                properties=ref.properties,
            )
        )

    if loc.verification_status == VerificationStatus.VERIFIED and v_detail == "UNVERIFIED":
        v_detail = "OFFICIALLY_VERIFIED"

    loc_geojson = safe_postgis_to_geojson(loc.location) or {"type": "Point", "coordinates": [0.0, 0.0]}
    bound_geojson = safe_postgis_to_geojson(loc.boundary) if loc.boundary is not None else None

    dist_pts: float | None = None
    safe_pts: float | None = None
    avail_pts: float | None = None
    feat_pts: float | None = None
    tot_pts: float | None = None

    if distance_km is not None:
        c_lat, c_lon = extract_location_coordinates(loc)
        dist_pts, safe_pts, avail_pts, feat_pts, tot_pts = calculate_specification_points(
            loc=loc,
            lat=c_lat,
            lon=c_lon,
            distance_km=distance_km,
        )

    return SafeLocationItem(
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
        distance_km=distance_km,
        distance_points=dist_pts,
        safety_points=safe_pts,
        availability_points=avail_pts,
        features_points=feat_pts,
        total_points=tot_pts,
        sources=sources,
        missing_information=extract_missing_information(loc),
        created_at=loc.created_at,
        updated_at=loc.updated_at,
    )


def get_nearby_safe_locations(
    session: Session,
    latitude: float,
    longitude: float,
    radius_km: float = 25.0,
    category: str | None = None,
    verification_status: VerificationStatus | None = None,
    limit: int = 50,
) -> NearbySafeLocationsResponse:
    """
    Retrieves safe locations and potential candidate destinations near requested coordinates
    using PostGIS spatial filtering and distance ordering.

    Supports category and verification filters while maintaining clear distinctions between
    officially verified shelters and unverified candidate facilities.
    """
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    dist_m = func.ST_Distance(SafeLocation.location.cast(Geography), user_geog).label("distance_m")
    search_radius_m = float(radius_km) * 1000.0

    # Purge any historical synthetic/placeholder locations
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
        logger.debug(f"Synthetic cleanup note in get_nearby: {exc}")

    # Discover candidate locations dynamically if none exist near user coordinates
    existing_count = session.scalar(
        select(func.count(SafeLocation.id)).where(
            func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m),
            ~SafeLocation.name.ilike("%refuge zone (%%"),
            ~SafeLocation.name.ilike("%assembly zone (%%"),
        )
    )
    try:
        cnt = int(existing_count) if existing_count is not None else 0
    except (TypeError, ValueError):
        cnt = 999

    if cnt < 3:
        try:
            discover_and_sync_candidate_locations(
                session=session,
                latitude=latitude,
                longitude=longitude,
                radius_km=float(radius_km),
            )
        except Exception:
            pass

    # Progressive search starting from 100 meters (0.1 km) near the user,
    # expanding until enough candidates are collected for diverse nearby choices.
    search_tiers_km = [0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, max(25.0, float(radius_km))]
    rows = []
    target_count = max(3, min(limit, 10))

    for tier_km in search_tiers_km:
        tier_m = tier_km * 1000.0
        tier_query = (
            select(SafeLocation, dist_m)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(
                func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, tier_m),
                ~SafeLocation.name.ilike("%refuge zone (%%"),
                ~SafeLocation.name.ilike("%assembly zone (%%"),
            )
        )
        if category:
            tier_query = tier_query.where(func.lower(SafeLocation.category) == category.strip().lower())
        if verification_status:
            tier_query = tier_query.where(SafeLocation.verification_status == verification_status)
        tier_query = tier_query.order_by(dist_m.asc()).limit(limit)

        tier_rows = session.execute(tier_query).all()
        if tier_rows:
            rows = tier_rows
            if len(rows) >= target_count:
                break

    if not rows:
        query = (
            select(SafeLocation, dist_m)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(
                func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m),
                ~SafeLocation.name.ilike("%refuge zone (%%"),
                ~SafeLocation.name.ilike("%assembly zone (%%"),
            )
        )

        if category:
            query = query.where(func.lower(SafeLocation.category) == category.strip().lower())

        if verification_status:
            query = query.where(SafeLocation.verification_status == verification_status)

        query = query.order_by(dist_m.asc()).limit(limit)
        rows = session.execute(query).all()

    items: list[SafeLocationItem] = []
    official_count = 0
    candidate_count = 0
    open_area_count = 0
    sources_seen: set[str] = set()

    for row in rows:
        loc: SafeLocation = row[0]
        dist_val_m: float = float(row[1]) if row[1] is not None else 0.0
        dist_val_km = round(dist_val_m / 1000.0, 2)

        item = format_safe_location_item(loc, distance_km=dist_val_km)
        items.append(item)

        if loc.designation_status == DesignationStatus.OFFICIAL and loc.verification_status == VerificationStatus.VERIFIED:
            official_count += 1
        else:
            candidate_count += 1

        if loc.category in ("PARK", "PLAYGROUND", "OPEN_GROUND"):
            open_area_count += 1

        for src in item.sources:
            sources_seen.add(src.data_source_name)

    return NearbySafeLocationsResponse(
        success=True,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        search_radius_km=radius_km,
        total_found=len(items),
        locations=items,
        official_shelters_count=official_count,
        candidate_locations_count=candidate_count,
        open_assembly_areas_count=open_area_count,
        data_sources_queried=sorted(sources_seen),
        data_integrity_notice=SAFE_LOCATION_DATA_INTEGRITY_NOTICE,
    )


def get_safe_location_by_id(
    session: Session,
    location_id: uuid.UUID,
) -> SafeLocationDetailResponse | None:
    """
    Retrieves full details, source references, and verification evidence for a specific location.
    """
    stmt = (
        select(SafeLocation)
        .options(
            selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
        )
        .where(SafeLocation.id == location_id)
    )
    loc = session.scalar(stmt)
    if loc is None:
        return None

    item = format_safe_location_item(loc)
    evidence: dict[str, Any] = {
        "designation_status": loc.designation_status.value,
        "verification_status": loc.verification_status.value,
        "total_source_references": len(loc.source_references),
        "source_references": [
            {
                "data_source_code": ref.data_source.code if ref.data_source else None,
                "data_source_name": ref.data_source.name if ref.data_source else None,
                "external_feature_id": ref.external_feature_id,
                "source_url": ref.source_url,
                "last_seen_at": ref.last_seen_at.isoformat() if ref.last_seen_at else None,
                "evidence_payload": ref.properties,
            }
            for ref in loc.source_references
        ],
    }

    return SafeLocationDetailResponse(
        success=True,
        location=item,
        verification_evidence=evidence,
        disaster_suitability_disclaimer=DISASTER_SUITABILITY_DISCLAIMER,
    )


def get_all_safe_locations(
    session: Session,
    category: str | None = None,
    verification_status: VerificationStatus | None = None,
    limit: int = 200,
) -> NearbySafeLocationsResponse:
    """
    Retrieves all registered safe shelters and candidate assembly areas across India.
    """
    query = (
        select(SafeLocation)
        .options(
            selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
        )
    )

    if category:
        query = query.where(func.lower(SafeLocation.category) == category.strip().lower())

    if verification_status:
        query = query.where(SafeLocation.verification_status == verification_status)

    query = query.order_by(SafeLocation.verification_status.asc(), SafeLocation.name.asc()).limit(limit)
    rows = session.scalars(query).all()

    items: list[SafeLocationItem] = []
    official_count = 0
    candidate_count = 0
    open_area_count = 0
    sources_seen: set[str] = set()

    for loc in rows:
        item = format_safe_location_item(loc, distance_km=None)
        items.append(item)

        if loc.designation_status == DesignationStatus.OFFICIAL and loc.verification_status == VerificationStatus.VERIFIED:
            official_count += 1
        else:
            candidate_count += 1

        if loc.category in ("PARK", "PLAYGROUND", "OPEN_GROUND"):
            open_area_count += 1

        for src in item.sources:
            sources_seen.add(src.data_source_name)

    return NearbySafeLocationsResponse(
        success=True,
        user_location=UserLocation(latitude=20.5937, longitude=78.9629),
        search_radius_km=3500.0,
        total_found=len(items),
        locations=items,
        official_shelters_count=official_count,
        candidate_locations_count=candidate_count,
        open_assembly_areas_count=open_area_count,
        data_sources_queried=sorted(sources_seen),
        data_integrity_notice=SAFE_LOCATION_DATA_INTEGRITY_NOTICE,
    )

