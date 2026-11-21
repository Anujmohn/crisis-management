from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.ingestion import IngestionSyncResponse
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Default NCS dissemination URL
NCS_RECENT_EARTHQUAKES_URL = (
    "https://riseq.seismo.gov.in/riseq/earthquake/recent_earthquake"
)

# Active emergency window for earthquake event status (48 hours)
ACTIVE_EVENT_WINDOW = timedelta(hours=48)


@dataclass
class NCSEarthquakeRecord:
    """Normalized in-memory record extracted from National Center for Seismology."""

    origin_time_utc: datetime
    latitude: float
    longitude: float
    depth_km: float | None
    magnitude: float | None
    region: str | None
    location_desc: str | None
    external_event_id: str
    source_url: str | None
    status: str
    raw_data: dict[str, Any]


def map_magnitude_to_severity(magnitude: float | None) -> SeverityLevel | None:
    """
    Maps earthquake magnitude (Richter/Moment magnitude) to standard SeverityLevel.
    Returns None if magnitude is unknown or missing.
    """
    if magnitude is None:
        return None
    if magnitude < 4.0:
        return SeverityLevel.LOW
    if magnitude < 5.0:
        return SeverityLevel.MODERATE
    if magnitude < 6.0:
        return SeverityLevel.HIGH
    if magnitude < 7.0:
        return SeverityLevel.SEVERE
    return SeverityLevel.EXTREME


def parse_ncs_origin_time(time_str: str) -> datetime | None:
    """
    Parses origin time strings from NCS format: 'YYYY-MM-DD HH:MM:SS IST'
    Returns a timezone-aware UTC datetime.
    """
    cleaned = time_str.strip()
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+IST$",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        # Fallback format without explicit IST string
        match = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})$", cleaned)
        if not match:
            return None

    try:
        dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=IST).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_ncs_html(
    html: str,
    max_records: int | None = None,
) -> tuple[list[NCSEarthquakeRecord], list[str]]:
    """
    Parses the HTML table from NCS recent_earthquake page.
    Extracts table with id 'archive_eqs' and parses rows safely.
    Returns a tuple of (valid_records, parsing_errors).
    """
    records: list[NCSEarthquakeRecord] = []
    errors: list[str] = []

    table_match = re.search(
        r'<table[^>]*id=\s*["\']archive_eqs["\'][^>]*>(.*?)</table>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not table_match:
        errors.append("Could not find earthquake data table 'archive_eqs' in HTML.")
        return records, errors

    table_html = table_match.group(1)
    tbody_match = re.search(r"<tbody>(.*?)</tbody>", table_html, re.DOTALL | re.IGNORECASE)
    rows_html = tbody_match.group(1) if tbody_match else table_html

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", rows_html, re.DOTALL | re.IGNORECASE)

    for row_idx, row in enumerate(rows):
        if max_records is not None and len(records) >= max_records:
            break

        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        if len(tds) < 7:
            # Not a data row
            continue

        clean_cols = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]

        time_str = clean_cols[0]
        origin_time_utc = parse_ncs_origin_time(time_str)
        if origin_time_utc is None:
            errors.append(f"Row {row_idx}: Invalid origin time format '{time_str}'.")
            continue

        # Latitude
        try:
            latitude = float(clean_cols[1])
            if not (-90.0 <= latitude <= 90.0):
                errors.append(f"Row {row_idx}: Latitude {latitude} out of bounds.")
                continue
        except (ValueError, TypeError):
            errors.append(f"Row {row_idx}: Unparseable latitude '{clean_cols[1]}'.")
            continue

        # Longitude
        try:
            longitude = float(clean_cols[2])
            if not (-180.0 <= longitude <= 180.0):
                errors.append(f"Row {row_idx}: Longitude {longitude} out of bounds.")
                continue
        except (ValueError, TypeError):
            errors.append(f"Row {row_idx}: Unparseable longitude '{clean_cols[2]}'.")
            continue

        # Depth (km)
        depth_km: float | None = None
        try:
            depth_km = float(clean_cols[3])
        except (ValueError, TypeError):
            depth_km = None

        # Magnitude
        magnitude: float | None = None
        try:
            magnitude = float(clean_cols[4])
        except (ValueError, TypeError):
            magnitude = None

        region = clean_cols[5] if len(clean_cols) > 5 and clean_cols[5] else None
        location_desc = clean_cols[6] if len(clean_cols) > 6 and clean_cols[6] else None

        # Event Link, Identifier, and Review Status
        link_match = re.search(r'href=["\']([^"\']+)["\']', row, re.IGNORECASE)
        source_url: str | None = None
        external_event_id: str | None = None
        event_status = "Reviewed"

        if link_match:
            source_url = link_match.group(1).strip()
            # Extract ID from /event/<ID>/<STATUS>
            id_match = re.search(r"/event/([^/\"\'\s]+)(?:/([^/\"\'\s]+))?", source_url)
            if id_match:
                external_event_id = id_match.group(1)
                if id_match.group(2):
                    event_status = id_match.group(2)

        # Fallback deterministic identifier if URL identifier not present
        if not external_event_id:
            origin_iso = origin_time_utc.isoformat()
            key = f"NCS_{origin_iso}_{latitude:.3f}_{longitude:.3f}"
            external_event_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

        raw_data = {
            "origin_time_ist": time_str,
            "origin_time_utc": origin_time_utc.isoformat(),
            "latitude": latitude,
            "longitude": longitude,
            "depth_km": depth_km,
            "magnitude": magnitude,
            "region": region,
            "location": location_desc,
            "details_url": source_url,
            "status": event_status,
        }

        records.append(
            NCSEarthquakeRecord(
                origin_time_utc=origin_time_utc,
                latitude=latitude,
                longitude=longitude,
                depth_km=depth_km,
                magnitude=magnitude,
                region=region,
                location_desc=location_desc,
                external_event_id=external_event_id,
                source_url=source_url,
                status=event_status,
                raw_data=raw_data,
            )
        )

    return records, errors


def fetch_ncs_earthquake_html(
    url: str = NCS_RECENT_EARTHQUAKES_URL,
    timeout_seconds: float = 15.0,
) -> str:
    """
    Fetches the live recent earthquake web page from National Center for Seismology.
    """
    ctx = ssl.create_default_context()
    # Official government portals occasionally use intermediate root certificates
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def sync_earthquakes_from_ncs(
    session: Session,
    html_content: str | None = None,
    max_records: int = 50,
    url: str = NCS_RECENT_EARTHQUAKES_URL,
) -> IngestionSyncResponse:
    """
    Synchronizes earthquakes from National Center for Seismology (NCS).
    Idempotent operation:
    - Creates new CrisisEvent & CrisisSourceReport for unseen earthquakes.
    - Updates existing records if data changed (e.g. reviewed vs preliminary).
    - Skips records already up to date.
    - Returns IngestionSyncResponse.
    """
    now = datetime.now(timezone.utc)

    # 1. Ensure DataSource entry exists in DB
    sync_data_sources(session)
    stmt = select(DataSource).where(DataSource.code == "NCS_SEISMO")
    data_source = session.scalar(stmt)
    if data_source is None:
        return IngestionSyncResponse(
            success=False,
            source="NCS",
            fetched=0,
            created=0,
            updated=0,
            skipped=0,
            failed=0,
            message="Data source 'NCS_SEISMO' not found in registry.",
            errors=["DataSource NCS_SEISMO missing"],
        )

    data_source.last_attempt_at = now

    # 2. Fetch HTML if not directly provided
    if html_content is None:
        try:
            html_content = fetch_ncs_earthquake_html(url=url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error_msg = f"Network failure fetching NCS earthquakes: {exc}"
            logger.error(error_msg)
            session.commit()
            return IngestionSyncResponse(
                success=False,
                source="NCS",
                fetched=0,
                created=0,
                updated=0,
                skipped=0,
                failed=0,
                message=error_msg,
                errors=[str(exc)],
            )

    # 3. Parse records
    parsed_records, parse_errors = parse_ncs_html(
        html_content,
        max_records=max_records,
    )
    fetched_count = len(parsed_records)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = len(parse_errors)

    # 4. Ingest parsed records into database
    for record in parsed_records:
        try:
            # Generate deterministic payload hash
            payload_str = json.dumps(record.raw_data, sort_keys=True)
            payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            # Check if source report already exists for this external_event_id
            report_stmt = select(CrisisSourceReport).where(
                CrisisSourceReport.data_source_id == data_source.id,
                CrisisSourceReport.external_event_id == record.external_event_id,
            )
            existing_report = session.scalar(report_stmt)

            # Determine title & description
            mag_str = (
                f"M {record.magnitude:.1f}"
                if record.magnitude is not None
                else "Unspecified Magnitude"
            )
            place = record.region or record.location_desc or "India Region"
            event_title = f"{mag_str} Earthquake - {place}"
            event_desc = (
                f"Earthquake of {mag_str} occurred on {record.raw_data['origin_time_ist']} "
                f"at depth of {record.depth_km or 'unknown'} km. "
                f"Region: {record.region or 'N/A'}. Location: {record.location_desc or 'N/A'}."
            )

            severity = map_magnitude_to_severity(record.magnitude)

            # Determine event status:
            # Active if within the last 48 hours, otherwise expired
            is_active = (now - record.origin_time_utc) <= ACTIVE_EVENT_WINDOW
            event_status = CrisisStatus.ACTIVE if is_active else CrisisStatus.EXPIRED

            expires_at = record.origin_time_utc + ACTIVE_EVENT_WINDOW

            if existing_report is not None:
                # Check if payload changed
                if existing_report.payload_hash == payload_hash:
                    # No data changed: touch last_seen_at on event
                    existing_event = session.get(CrisisEvent, existing_report.crisis_event_id)
                    if existing_event is not None:
                        existing_event.last_seen_at = now
                    skipped_count += 1
                else:
                    # Payload updated (e.g. preliminary to reviewed, or revised magnitude)
                    existing_report.source_severity = (
                        str(record.magnitude) if record.magnitude is not None else None
                    )
                    existing_report.source_status = record.status
                    existing_report.source_title = event_title
                    existing_report.source_description = record.location_desc
                    existing_report.raw_payload = record.raw_data
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
                postgis_point = geojson_to_postgis(
                    {
                        "type": "Point",
                        "coordinates": [record.longitude, record.latitude],
                    }
                )

                crisis_event = CrisisEvent(
                    id=uuid.uuid4(),
                    crisis_type=CrisisType.EARTHQUAKE,
                    title=event_title,
                    description=event_desc,
                    severity=severity,
                    status=event_status,
                    location=postgis_point,
                    affected_area=None,
                    started_at=record.origin_time_utc,
                    expires_at=expires_at,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(crisis_event)

                # Create associated CrisisSourceReport
                source_report = CrisisSourceReport(
                    id=uuid.uuid4(),
                    crisis_event=crisis_event,
                    data_source=data_source,
                    external_event_id=record.external_event_id,
                    source_url=record.source_url,
                    source_title=event_title,
                    source_description=record.location_desc,
                    source_severity=(
                        str(record.magnitude) if record.magnitude is not None else None
                    ),
                    source_status=record.status,
                    issued_at=record.origin_time_utc,
                    valid_until=expires_at,
                    fetched_at=now,
                    raw_payload=record.raw_data,
                    payload_hash=payload_hash,
                )
                session.add(source_report)
                created_count += 1

        except Exception as exc:
            logger.exception("Error ingesting earthquake record: %s", exc)
            failed_count += 1
            parse_errors.append(f"Record {record.external_event_id}: {exc}")

    data_source.last_successful_fetch = now
    session.commit()

    return IngestionSyncResponse(
        success=True,
        source="NCS",
        fetched=fetched_count,
        created=created_count,
        updated=updated_count,
        skipped=skipped_count,
        failed=failed_count,
        message=f"Synchronized {fetched_count} earthquake records from NCS.",
        errors=parse_errors[:10],  # Return up to 10 sample errors if any
    )
