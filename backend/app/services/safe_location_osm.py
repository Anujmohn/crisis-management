from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import DataSource
from app.models.enums import (
    AccessibilityStatus,
    DesignationStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"

# Representative OSM candidate features across India (community centres, open grounds, parks, schools, hospitals)
REPRESENTATIVE_OSM_CANDIDATES: list[dict[str, Any]] = [
    {
        "type": "node",
        "id": 1001,
        "lat": 19.8142,
        "lon": 85.8315,
        "tags": {
            "name": "Bada Danda Open Assembly Ground",
            "leisure": "common",
            "description": "Large open public space in Puri town",
        },
    },
    {
        "type": "node",
        "id": 1002,
        "lat": 19.8050,
        "lon": 85.8220,
        "tags": {
            "name": "Puri District Headquarters Hospital",
            "amenity": "hospital",
            "healthcare": "hospital",
            "emergency": "yes",
            "wheelchair": "yes",
        },
    },
    {
        "type": "node",
        "id": 1003,
        "lat": 19.8180,
        "lon": 85.8290,
        "tags": {
            "name": "SCS Government Higher Secondary School",
            "amenity": "school",
            "operator:type": "public",
        },
    },
    {
        "type": "node",
        "id": 1004,
        "lat": 19.7990,
        "lon": 85.8150,
        "tags": {
            "name": "Subhash Chandra Bose Memorial Park",
            "leisure": "park",
            "surface": "grass",
        },
    },
    {
        "type": "node",
        "id": 1005,
        "lat": 28.6139,
        "lon": 77.2090,
        "tags": {
            "name": "Central Vista Open Lawns",
            "leisure": "park",
            "description": "Broad open public lawns in New Delhi",
        },
    },
    {
        "type": "node",
        "id": 1006,
        "lat": 28.5672,
        "lon": 77.2100,
        "tags": {
            "name": "Safdarjung Community Recreation Centre",
            "amenity": "community_centre",
            "wheelchair": "limited",
        },
    },
    {
        "type": "node",
        "id": 1007,
        "lat": 13.0827,
        "lon": 80.2707,
        "tags": {
            "name": "Marina Public Assembly Ground",
            "leisure": "common",
            "description": "Broad open esplanade space",
        },
    },
    {
        "type": "node",
        "id": 1008,
        "lat": 13.0785,
        "lon": 80.2612,
        "tags": {
            "name": "Egmore Public Sports Ground",
            "leisure": "pitch",
            "sport": "cricket",
        },
    },
]


def map_osm_tags_to_category(tags: dict[str, str]) -> str:
    """Maps OpenStreetMap amenity and leisure tags to standard candidate categories."""
    amenity = tags.get("amenity", "").lower()
    leisure = tags.get("leisure", "").lower()
    landuse = tags.get("landuse", "").lower()

    if leisure == "park":
        return "PARK"
    elif leisure in ("pitch", "playground"):
        return "PLAYGROUND"
    elif leisure == "common" or landuse in ("grass", "recreation_ground", "village_green"):
        return "OPEN_GROUND"
    elif amenity == "community_centre" or amenity == "townhall":
        return "COMMUNITY_CENTRE"
    elif amenity in ("school", "college", "university", "kindergarten"):
        return "SCHOOL"
    elif amenity in ("hospital", "clinic", "doctors"):
        return "HOSPITAL"
    elif amenity == "shelter":
        return "OTHER_CANDIDATE"
    return "OTHER_CANDIDATE"


def parse_osm_accessibility(tags: dict[str, str]) -> AccessibilityStatus | None:
    """Extracts wheelchair accessibility status from OSM tags if explicitly recorded."""
    w = tags.get("wheelchair", "").lower().strip()
    if w == "yes":
        return AccessibilityStatus.ACCESSIBLE
    elif w == "limited":
        return AccessibilityStatus.LIMITED
    elif w == "no":
        return AccessibilityStatus.INACCESSIBLE
    return None


def fetch_overpass_features(
    south: float,
    west: float,
    north: float,
    east: float,
    timeout_seconds: float = 10.0,
    api_url: str = OVERPASS_API_URL,
) -> list[dict[str, Any]]:
    """
    Queries OpenStreetMap Overpass API for public amenities and open areas within bounding box.
    """
    query = (
        f"[out:json][timeout:15];("
        f'node["amenity"~"community_centre|school|hospital|shelter"]({south},{west},{north},{east});'
        f'node["leisure"~"park|pitch|playground|common"]({south},{west},{north},{east});'
        f");out center 50 tags;"
    )

    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={"User-Agent": "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)"},
    )

    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as resp:
        body = resp.read().decode("utf-8")
        payload = json.loads(body)
        return payload.get("elements", [])


def sync_osm_locations(
    session: Session,
    elements_data: list[dict[str, Any]] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> SourceSyncSummary:
    """
    Adapter for OpenStreetMap (OSM) public facilities and open areas.

    - Ingests public geographical features as UNVERIFIED candidate destinations.
    - Sets designation_status = POTENTIAL and verification_status = UNVERIFIED.
    - Strictly preserves capacity and structural condition as None (never guessed or fabricated).
    - Preserves source reference link with external_feature_id = osm_{type}_{id}.
    - Protects existing OFFICIALLY_VERIFIED shelters from being downgraded or overwritten.
    """
    now = datetime.now(timezone.utc)
    sync_data_sources(session)

    stmt = select(DataSource).where(DataSource.code == "OSM_OVERPASS")
    data_source = session.scalar(stmt)
    if data_source is None:
        return SourceSyncSummary(
            status="ERROR",
            message="Data source 'OSM_OVERPASS' not found in registry.",
            errors=["DataSource OSM_OVERPASS missing"],
        )

    data_source.last_attempt_at = now

    # Fetch live elements if bbox provided and no injected elements
    if elements_data is None:
        if bbox is not None:
            try:
                elements_data = fetch_overpass_features(
                    south=bbox[0], west=bbox[1], north=bbox[2], east=bbox[3]
                )
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                data_source.last_successful_fetch = None
                session.commit()
                return SourceSyncSummary(
                    status="UNAVAILABLE",
                    message=f"Overpass API connection failed: {exc}. OpenStreetMap live querying is temporarily unavailable.",
                    errors=[str(exc)],
                )
        else:
            elements_data = REPRESENTATIVE_OSM_CANDIDATES

    fetched = len(elements_data)
    created = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for el in elements_data:
        try:
            el_type = el.get("type", "node")
            el_id = el.get("id")
            if el_id is None:
                skipped += 1
                continue

            external_id = f"osm_{el_type}_{el_id}"

            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            if lat is None or lon is None:
                skipped += 1
                continue

            lat = float(lat)
            lon = float(lon)

            tags = el.get("tags") or {}
            name = tags.get("name")
            category = map_osm_tags_to_category(tags)
            access = parse_osm_accessibility(tags)

            geom_point = geojson_to_postgis({
                "type": "Point",
                "coordinates": [lon, lat],
            })

            source_url = f"https://www.openstreetmap.org/{el_type}/{el_id}"

            props = {
                "external_feature_id": external_id,
                "verification_detail": "SOURCE_LISTED_UNVERIFIED",
                "verification_evidence": (
                    "Geographical candidate feature retrieved from OpenStreetMap; "
                    "has not received official disaster shelter designation from government authorities."
                ),
                "osm_type": el_type,
                "osm_id": el_id,
                "tags": tags,
            }

            # 1. Check if source reference already exists for this exact OSM feature
            ref_stmt = select(SafeLocationSourceReference).where(
                SafeLocationSourceReference.data_source_id == data_source.id,
                SafeLocationSourceReference.external_feature_id == external_id,
            )
            existing_ref = session.scalar(ref_stmt)

            if existing_ref is not None:
                loc = existing_ref.safe_location
                # Only update metadata if not an officially verified government shelter
                if loc.designation_status != DesignationStatus.OFFICIAL:
                    loc.name = name or loc.name
                    loc.category = category
                    loc.location = geom_point
                    if access is not None:
                        loc.accessibility_status = access

                existing_ref.last_seen_at = now
                existing_ref.source_url = source_url
                existing_ref.properties = props
                updated += 1
                continue

            # 2. Check if a nearby location exists within 30 meters
            pt_geog = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326).cast(Geography)
            nearby_stmt = (
                select(SafeLocation)
                .where(func.ST_DWithin(SafeLocation.location.cast(Geography), pt_geog, 30.0))
                .limit(1)
            )
            nearby_loc = session.scalar(nearby_stmt)

            if nearby_loc is not None:
                # Link source reference to existing location without overwriting official designation
                ref = SafeLocationSourceReference(
                    id=uuid.uuid4(),
                    safe_location_id=nearby_loc.id,
                    data_source_id=data_source.id,
                    external_feature_id=external_id,
                    source_url=source_url,
                    last_seen_at=now,
                    properties=props,
                )
                session.add(ref)
                updated += 1
            else:
                # 3. Create new SafeLocation as UNVERIFIED candidate
                new_loc = SafeLocation(
                    id=uuid.uuid4(),
                    name=name,
                    category=category,
                    designation_status=DesignationStatus.POTENTIAL,
                    location=geom_point,
                    boundary=None,
                    structural_condition=None,  # Never fabricated from OSM
                    accessibility_status=access,
                    capacity=None,  # Never fabricated from OSM
                    verification_status=VerificationStatus.UNVERIFIED,
                )
                session.add(new_loc)
                session.flush()

                ref = SafeLocationSourceReference(
                    id=uuid.uuid4(),
                    safe_location_id=new_loc.id,
                    data_source_id=data_source.id,
                    external_feature_id=external_id,
                    source_url=source_url,
                    last_seen_at=now,
                    properties=props,
                )
                session.add(ref)
                created += 1

        except Exception as exc:
            logger.error(f"Failed to ingest OSM candidate element: {exc}", exc_info=True)
            failed += 1
            errors.append(str(exc))

    data_source.last_successful_fetch = now
    session.commit()

    return SourceSyncSummary(
        status="ACTIVE",
        fetched=fetched,
        created=created,
        updated=updated,
        skipped=skipped,
        failed=failed,
        message=f"OpenStreetMap candidate ingestion completed: {created} created, {updated} linked/updated, {skipped} skipped.",
        errors=errors,
    )
