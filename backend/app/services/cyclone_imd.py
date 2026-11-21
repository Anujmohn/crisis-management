from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

# Official IMD GeoServer WFS endpoint for Cyclone Tracks
IMD_WFS_CYCLONE_TRACK_URL = (
    "https://reactjs.imd.gov.in/geoserver/imd/ows?"
    "service=WFS&version=1.1.0&request=GetFeature&"
    "typename=imd:Cyclone_Track_V&srsname=EPSG:4326&outputFormat=application/json"
)

# Official IMD Mausam Cyclone Information Web Portal
IMD_MAUSAM_CYCLONE_PORTAL_URL = "https://mausam.imd.gov.in/responsive/cycloneinformation.php"


def fetch_imd_cyclone_wfs(
    url: str = IMD_WFS_CYCLONE_TRACK_URL,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """
    Fetches the live GeoJSON FeatureCollection from IMD's official GeoServer WFS for Cyclone Tracks.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content)


def fetch_imd_mausam_bulletins(
    url: str = IMD_MAUSAM_CYCLONE_PORTAL_URL,
    timeout_seconds: float = 10.0,
) -> dict[str, str]:
    """
    Scrapes active cyclone bulletin PDF and track chart links from IMD's Mausam cyclone portal.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    bulletins: dict[str, str] = {}
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            # Extract national bulletin PDF
            nat_match = re.search(r'openPDF\(\s*["\']National Bulletin["\']\s*,\s*["\']([^"\']+\.pdf)["\']', html)
            if nat_match:
                bulletins["national_bulletin_pdf"] = nat_match.group(1)

            # Extract tropical weather outlook PDF
            two_match = re.search(r'openPDF\(\s*["\']Tropical Weather Outlook["\']\s*,\s*["\']([^"\']+\.pdf)["\']', html)
            if two_match:
                bulletins["tropical_weather_outlook_pdf"] = two_match.group(1)

            # Extract track chart image
            track_match = re.search(r'openImage\(\s*["\']Track of Cyclonic Disturbance["\']\s*,\s*["\']([^"\']+\.(?:png|jpg|jpeg))["\']', html)
            if track_match:
                bulletins["track_chart_img"] = track_match.group(1)

            # Extract wind warning chart
            wind_match = re.search(r'openImage\(\s*["\']Wind Warning["\']\s*,\s*["\']([^"\']+\.(?:png|jpg|jpeg))["\']', html)
            if wind_match:
                bulletins["wind_warning_chart_img"] = wind_match.group(1)
    except Exception as exc:
        logger.debug(f"Unable to scrape Mausam bulletin metadata: {exc}")

    return bulletins


def create_track_affected_area(coordinates: list[list[float]]) -> MultiPolygon | None:
    """
    Generates a MultiPolygon affected corridor buffering the cyclone track coordinates.
    """
    if not coordinates or len(coordinates) < 1:
        return None
    try:
        if len(coordinates) == 1:
            pt = Point(coordinates[0][0], coordinates[0][1])
            buffered = pt.buffer(0.75)  # ~75 km radius
        else:
            line = LineString([[c[0], c[1]] for c in coordinates])
            buffered = line.buffer(0.60)  # ~60 km swath corridor

        if isinstance(buffered, Polygon):
            return MultiPolygon([buffered])
        elif isinstance(buffered, MultiPolygon):
            return buffered
        return None
    except Exception as exc:
        logger.warning(f"Failed to generate track affected area: {exc}")
        return None


def sync_imd_cyclone_data(
    session: Session,
    geojson_data: dict[str, Any] | None = None,
    bulletins_data: dict[str, str] | None = None,
    max_records: int | None = 10,
) -> SourceSyncSummary:
    """
    Adapter for India Meteorological Department (IMD) / RSMC Cyclone Tracks & Warnings.

    - Retrieves live Cyclone_Track_V GeoJSON from IMD's GeoServer WFS.
    - Retrieves active bulletin references from IMD Mausam.
    - Extracts current cyclone position and forecast track waypoints.
    - Preserves forecast track points in CrisisSourceReport.raw_payload without overwriting current position.
    - Normalizes into CrisisEvent, CrisisSourceReport, and HazardObservation.
    - Enforces idempotency via external_event_id + payload_hash.
    """
    now = datetime.now(timezone.utc)

    # 1. Ensure DataSource entry exists in DB
    sync_data_sources(session)
    stmt = select(DataSource).where(DataSource.code == "IMD_WEATHER_API")
    data_source = session.scalar(stmt)
    if data_source is None:
        return SourceSyncSummary(
            status="ERROR",
            message="Data source 'IMD_WEATHER_API' not found in registry.",
            errors=["DataSource IMD_WEATHER_API missing"],
        )

    data_source.last_attempt_at = now

    # 2. Retrieve live GeoJSON if not injected
    if geojson_data is None:
        try:
            geojson_data = fetch_imd_cyclone_wfs()
        except urllib.error.HTTPError as exc:
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=f"IMD GeoServer returned HTTP {exc.code}: {exc.reason}",
                errors=[str(exc)],
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=f"Failed to connect to IMD GeoServer WFS: {exc}",
                errors=[str(exc)],
            )
        except Exception as exc:
            session.commit()
            return SourceSyncSummary(
                status="ERROR",
                message=f"Unexpected error querying IMD Cyclone WFS: {exc}",
                errors=[str(exc)],
            )

    # 3. Retrieve bulletins if not provided
    if bulletins_data is None:
        bulletins_data = fetch_imd_mausam_bulletins()

    features = geojson_data.get("features", [])
    if not features:
        data_source.last_successful_fetch = now
        session.commit()
        return SourceSyncSummary(
            status="ACTIVE",
            fetched=0,
            created=0,
            updated=0,
            skipped=0,
            failed=0,
            message="No active cyclone tracks currently published on IMD GeoServer.",
        )

    if max_records and max_records > 0:
        features = features[:max_records]

    fetched = len(features)
    created = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for feature in features:
        try:
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            feat_id = feature.get("id") or properties.get("id", "track")
            cyclone_id = properties.get("cyclone_id", 0)
            cyclone_type = properties.get("cyclone_type", "cyclone")

            coords = geometry.get("coordinates") or []
            if not coords:
                skipped += 1
                continue

            # Deterministic external identifier
            external_event_id = f"IMD_CYCLONE_{cyclone_id}_{feat_id}"

            # Validate coordinates: must be [lon, lat] pairs within valid bounds
            valid_coords: list[list[float]] = []
            for pt in coords:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    lon, lat = float(pt[0]), float(pt[1])
                    if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
                        valid_coords.append([lon, lat])

            if not valid_coords:
                skipped += 1
                continue

            # Current position is the initial/latest known track coordinate
            current_lon, current_lat = valid_coords[0][0], valid_coords[0][1]
            current_geom = geojson_to_postgis({
                "type": "Point",
                "coordinates": [current_lon, current_lat],
            })

            # MultiPolygon affected corridor along the track
            affected_multi_poly = create_track_affected_area(valid_coords)
            affected_geom = (
                geojson_to_postgis(mapping(affected_multi_poly))
                if affected_multi_poly is not None
                else None
            )

            # Preserve forecast track waypoints in structured raw payload
            forecast_track_waypoints = [
                {
                    "waypoint_index": idx,
                    "longitude": pt[0],
                    "latitude": pt[1],
                    "position_type": "current" if idx == 0 else "forecast",
                }
                for idx, pt in enumerate(valid_coords)
            ]

            canonical_payload = {
                "source": "IMD_RSMC_NEW_DELHI",
                "external_event_id": external_event_id,
                "cyclone_id": cyclone_id,
                "cyclone_type": cyclone_type,
                "geometry": geometry,
                "current_position": {"longitude": current_lon, "latitude": current_lat},
                "forecast_track": forecast_track_waypoints,
                "bulletins": bulletins_data,
            }

            payload_str = json.dumps(canonical_payload, sort_keys=True, default=str)
            payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            title = (
                f"IMD Tropical Cyclone Advisory — System {cyclone_id} "
                f"({cyclone_type.capitalize()} Track)"
            )
            description = (
                f"Official tropical cyclone tracking system {cyclone_id} monitored by IMD RSMC New Delhi. "
                f"Current center at {current_lat:.2f}°N, {current_lon:.2f}°E with {len(valid_coords)} track waypoints."
            )

            # Check if source report already exists
            report_stmt = select(CrisisSourceReport).where(
                CrisisSourceReport.data_source_id == data_source.id,
                CrisisSourceReport.external_event_id == external_event_id,
            )
            existing_report = session.scalar(report_stmt)

            if existing_report is not None:
                # Update existing record
                crisis_event = existing_report.crisis_event
                if existing_report.payload_hash == payload_hash:
                    # Unchanged: update touch timestamp
                    crisis_event.last_seen_at = now
                    skipped += 1
                else:
                    # Revised: update fields
                    existing_report.source_title = title
                    existing_report.source_description = description
                    existing_report.source_status = "ACTIVE"
                    existing_report.raw_payload = canonical_payload
                    existing_report.payload_hash = payload_hash
                    existing_report.fetched_at = now
                    if bulletins_data.get("national_bulletin_pdf"):
                        existing_report.source_url = bulletins_data["national_bulletin_pdf"]

                    crisis_event.title = title
                    crisis_event.description = description
                    crisis_event.location = current_geom
                    crisis_event.affected_area = affected_geom
                    crisis_event.last_seen_at = now
                    updated += 1
            else:
                # Create new CrisisEvent
                crisis_event = CrisisEvent(
                    id=uuid.uuid4(),
                    crisis_type=CrisisType.CYCLONE,
                    title=title,
                    description=description,
                    severity=SeverityLevel.SEVERE,
                    status=CrisisStatus.ACTIVE,
                    location=current_geom,
                    affected_area=affected_geom,
                    started_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(crisis_event)
                session.flush()

                # Create CrisisSourceReport
                source_report = CrisisSourceReport(
                    id=uuid.uuid4(),
                    crisis_event_id=crisis_event.id,
                    data_source_id=data_source.id,
                    external_event_id=external_event_id,
                    source_url=bulletins_data.get("national_bulletin_pdf") or IMD_MAUSAM_CYCLONE_PORTAL_URL,
                    source_title=title,
                    source_description=description,
                    source_severity="Severe",
                    source_status="ACTIVE",
                    issued_at=now,
                    fetched_at=now,
                    raw_payload=canonical_payload,
                    payload_hash=payload_hash,
                )
                session.add(source_report)

                # Store current position in HazardObservation
                pos_obs = HazardObservation(
                    id=uuid.uuid4(),
                    crisis_event_id=crisis_event.id,
                    data_source_id=data_source.id,
                    observation_type="CYCLONE_POSITION",
                    value_numeric=Decimal(str(cyclone_id)),
                    value_text=f"Lat {current_lat:.2f}, Lon {current_lon:.2f}",
                    unit="coordinates",
                    geometry=current_geom,
                    observed_at=now,
                    ingested_at=now,
                    properties={
                        "cyclone_id": cyclone_id,
                        "cyclone_type": cyclone_type,
                        "total_track_points": len(valid_coords),
                    },
                )
                session.add(pos_obs)
                created += 1

        except Exception as exc:
            logger.error(f"Failed to process IMD cyclone feature: {exc}", exc_info=True)
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
        message=f"IMD cyclone ingestion completed: {created} created, {updated} updated, {skipped} skipped.",
        errors=errors,
    )
