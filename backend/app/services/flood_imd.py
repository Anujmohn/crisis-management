from __future__ import annotations

import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Official IMD WFS GeoJSON endpoint for district-wise severe weather & nowcasts
IMD_WFS_NOWCAST_URL = (
    "https://reactjs.imd.gov.in/geoserver/imd/ows?"
    "service=WFS&version=1.1.0&request=GetFeature&"
    "typename=imd:NowcastWarningDistrict&srsname=EPSG:4326&outputFormat=application/json"
)

# Color to Severity mapping
# 1: Green (No warning), 2: Yellow (Watch), 3: Orange (Alert - Heavy rain), 4: Red (Warning - Extremely heavy rain/flood)
IMD_COLOR_SEVERITY = {
    4: (SeverityLevel.EXTREME, "Red Warning"),
    3: (SeverityLevel.SEVERE, "Orange Alert"),
    2: (SeverityLevel.MODERATE, "Yellow Watch"),
}

IMD_CATEGORY_DESCRIPTIONS = {
    "cat1": "No Warning",
    "cat2": "Light rain: < 5 mm/hr",
    "cat7": "Moderate rain: 5-15 mm/hr",
    "cat12": "Heavy rain: > 15 mm/hr",
    "cat14": "Severe Thunderstorms (62-87 kmph gusts)",
    "cat15": "Very Severe Thunderstorms (> 87 kmph gusts)",
    "cat17": "Thunderstorms with Hail",
}


def parse_imd_timestamps(date_str: str, toi_str: str, vupto_str: str) -> tuple[datetime, datetime]:
    """
    Parses IMD Date ('YYYY-MM-DD'), toi ('HHMM'), vupto ('HHMM') into UTC datetimes.
    """
    try:
        base_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        base_date = datetime.now(timezone.utc).date()

    # Time of issue
    try:
        toi_clean = toi_str.strip().zfill(4)
        t_issue = time(hour=int(toi_clean[:2]), minute=int(toi_clean[2:]))
        dt_issue_ist = datetime.combine(base_date, t_issue).replace(tzinfo=IST)
        dt_issue_utc = dt_issue_ist.astimezone(timezone.utc)
    except (ValueError, TypeError):
        dt_issue_utc = datetime.now(timezone.utc)

    # Valid upto
    try:
        vupto_clean = vupto_str.strip().zfill(4)
        t_valid = time(hour=int(vupto_clean[:2]), minute=int(vupto_clean[2:]))
        dt_valid_ist = datetime.combine(base_date, t_valid).replace(tzinfo=IST)
        if t_valid < t_issue:
            # Crosses midnight
            dt_valid_ist += timedelta(days=1)
        dt_valid_utc = dt_valid_ist.astimezone(timezone.utc)
    except (ValueError, TypeError):
        dt_valid_utc = dt_issue_utc + timedelta(hours=3)

    return dt_issue_utc, dt_valid_utc


def fetch_imd_wfs_geojson(url: str = IMD_WFS_NOWCAST_URL, timeout_seconds: float = 20.0) -> dict[str, Any]:
    """
    Fetches the live GeoJSON FeatureCollection from IMD's official GeoServer WFS.
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
        charset = resp.headers.get_content_charset() or "utf-8"
        raw_text = resp.read().decode(charset, errors="ignore")
        return json.loads(raw_text)


def sync_imd_flood_data(
    session: Session,
    geojson_data: dict[str, Any] | None = None,
    url: str = IMD_WFS_NOWCAST_URL,
    max_records: int | None = None,
) -> SourceSyncSummary:
    """
    Adapter for India Meteorological Department (IMD) heavy rainfall & nowcast flood alerts.

    - Fetches WFS GeoJSON from IMD GeoServer.
    - Validates district boundary geometries and warning categories.
    - Creates CrisisEvent & CrisisSourceReport for active warning districts (Color >= 2 or cat12).
    - Preserves district MultiPolygon in affected_area and centroid in location.
    - Ingests HazardObservation for rainfall intensity metrics.
    - Idempotently prevents duplicate events using external_event_id.
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

    # 2. Fetch GeoJSON if not directly supplied
    if geojson_data is None:
        try:
            geojson_data = fetch_imd_wfs_geojson(url=url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            error_msg = f"Network failure fetching IMD GeoServer nowcasts: {exc}"
            logger.error(error_msg)
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=error_msg,
                errors=[str(exc)],
            )

    features = geojson_data.get("features", [])
    if max_records is not None:
        features = features[:max_records]

    fetched_count = len(features)
    created_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    # 3. Process features
    for idx, feature in enumerate(features):
        try:
            props = feature.get("properties", {})
            geom_raw = feature.get("geometry")

            district = props.get("District") or props.get("State_District") or "Unknown District"
            state = props.get("State") or "India"
            district_id = props.get("id") or props.get("Fid") or idx
            date_str = props.get("Date") or now.strftime("%Y-%m-%d")
            toi = str(props.get("toi", "0000"))
            vupto = str(props.get("vupto", "0300"))
            color = int(props.get("Color") or 1)
            cat12 = props.get("cat12") == 1 or props.get("cat12") == "1"

            # Check if this district has an active flood/rain warning (Color >= 2 or Heavy rain cat12)
            has_warning = color >= 2 or cat12
            if not has_warning:
                # Normal calm condition: skip creating event, increment skipped
                skipped_count += 1
                continue

            # Deterministic external event ID
            date_compact = date_str.replace("-", "")
            external_event_id = f"IMD_NOWCAST_{district_id}_{date_compact}_{toi}"

            # Calculate timestamps
            dt_issue_utc, dt_valid_utc = parse_imd_timestamps(date_str, toi, vupto)

            # Determine severity
            severity, severity_label = IMD_COLOR_SEVERITY.get(
                color, (SeverityLevel.MODERATE, "Yellow Watch")
            )
            if cat12 and severity in (SeverityLevel.LOW, SeverityLevel.MODERATE):
                severity = SeverityLevel.SEVERE
                severity_label = "Heavy Rainfall Warning (>15 mm/hr)"

            # Construct titles & descriptions
            active_cats = [
                desc for k, desc in IMD_CATEGORY_DESCRIPTIONS.items() if props.get(k)
            ]
            cats_summary = "; ".join(active_cats) if active_cats else "Heavy precipitation expected"

            event_title = f"IMD {severity_label}: Flood & Rain Hazard - {district.title()}, {state.title()}"
            event_desc = (
                f"IMD District Nowcast Warning for {district.title()}, {state.title()}.\n"
                f"Severity: {severity_label}. Conditions: {cats_summary}.\n"
                f"Issued: {dt_issue_utc.isoformat()} (UTC). Valid until: {dt_valid_utc.isoformat()} (UTC)."
            )

            # Parse Geometry (MultiPolygon)
            postgis_poly = None
            postgis_point = None
            if geom_raw:
                try:
                    # Validate geometry
                    sh_geom = shape(geom_raw)
                    centroid = sh_geom.centroid
                    if -90.0 <= centroid.y <= 90.0 and -180.0 <= centroid.x <= 180.0:
                        postgis_point = geojson_to_postgis(
                            {"type": "Point", "coordinates": [centroid.x, centroid.y]}
                        )
                    # Convert to MultiPolygon if single Polygon
                    if geom_raw.get("type") == "Polygon":
                        multi_poly = {
                            "type": "MultiPolygon",
                            "coordinates": [geom_raw["coordinates"]],
                        }
                        postgis_poly = geojson_to_postgis(multi_poly)
                    elif geom_raw.get("type") == "MultiPolygon":
                        postgis_poly = geojson_to_postgis(geom_raw)
                except Exception as g_exc:
                    logger.warning("Geometry parse failed for district %s: %s", district, g_exc)

            # Payload hash for change detection
            payload_str = json.dumps(props, sort_keys=True)
            payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            # Event status based on validity window
            is_active = now <= dt_valid_utc
            event_status = CrisisStatus.ACTIVE if is_active else CrisisStatus.EXPIRED

            # Check if source report already exists
            report_stmt = select(CrisisSourceReport).where(
                CrisisSourceReport.data_source_id == data_source.id,
                CrisisSourceReport.external_event_id == external_event_id,
            )
            existing_report = session.scalar(report_stmt)

            if existing_report is not None:
                if existing_report.payload_hash == payload_hash:
                    # Unchanged: update last_seen_at
                    existing_event = session.get(CrisisEvent, existing_report.crisis_event_id)
                    if existing_event is not None:
                        existing_event.last_seen_at = now
                    skipped_count += 1
                else:
                    # Update report & event
                    existing_report.source_severity = severity_label
                    existing_report.source_title = event_title
                    existing_report.source_description = cats_summary
                    existing_report.raw_payload = props
                    existing_report.payload_hash = payload_hash
                    existing_report.fetched_at = now

                    existing_event = session.get(CrisisEvent, existing_report.crisis_event_id)
                    if existing_event is not None:
                        existing_event.title = event_title
                        existing_event.description = event_desc
                        existing_event.severity = severity
                        existing_event.status = event_status
                        existing_event.last_seen_at = now

                    updated_count += 1
            else:
                # Create new CrisisEvent
                crisis_event = CrisisEvent(
                    id=uuid.uuid4(),
                    crisis_type=CrisisType.FLOOD,
                    title=event_title,
                    description=event_desc,
                    severity=severity,
                    status=event_status,
                    location=postgis_point,
                    affected_area=postgis_poly,
                    started_at=dt_issue_utc,
                    expires_at=dt_valid_utc,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(crisis_event)

                # Create CrisisSourceReport
                source_report = CrisisSourceReport(
                    id=uuid.uuid4(),
                    crisis_event=crisis_event,
                    data_source=data_source,
                    external_event_id=external_event_id,
                    source_url="https://mausam.imd.gov.in/responsive/districtWiseNowcastGIS.php",
                    source_title=event_title,
                    source_description=cats_summary,
                    source_severity=severity_label,
                    source_status="Active" if is_active else "Expired",
                    issued_at=dt_issue_utc,
                    valid_until=dt_valid_utc,
                    fetched_at=now,
                    raw_payload=props,
                    payload_hash=payload_hash,
                )
                session.add(source_report)

                # Create HazardObservation for rainfall intensity
                intensity_val = Decimal("15.0") if cat12 else (Decimal("5.0") if props.get("cat7") else Decimal("1.0"))
                hazard_obs = HazardObservation(
                    id=uuid.uuid4(),
                    crisis_event=crisis_event,
                    data_source=data_source,
                    observation_type="RAINFALL_INTENSITY",
                    value_numeric=intensity_val,
                    value_text=cats_summary,
                    unit="mm/hr",
                    geometry=postgis_point,
                    observed_at=dt_issue_utc,
                    ingested_at=now,
                    properties={
                        "district": district,
                        "state": state,
                        "color_code": color,
                        "external_event_id": external_event_id,
                    },
                )
                session.add(hazard_obs)

                created_count += 1

        except Exception as exc:
            logger.exception("Error processing IMD nowcast feature: %s", exc)
            failed_count += 1
            errors.append(f"District {props.get('District', idx)}: {exc}")

    data_source.last_successful_fetch = now
    session.commit()

    return SourceSyncSummary(
        status="ACTIVE",
        fetched=fetched_count,
        created=created_count,
        updated=updated_count,
        skipped=skipped_count,
        failed=failed_count,
        message=f"Synchronized {fetched_count} IMD district nowcasts.",
        errors=errors[:10],
    )
