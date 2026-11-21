from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

SACHET_ALL_INDIA_RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
SACHET_XML_ALERT_BASE = "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier="

# Keywords that indicate cyclone, coastal, gale, or oceanic hazards
CYCLONE_KEYWORDS = (
    "cyclon",
    "scs",
    "gale",
    "squall",
    "storm",
    "depression",
    "high wave",
    "surge",
    "wind speed",
    "సముద్రం",
    "అలల",
    "incois",
    "ocean",
)

SEVERITY_MAPPING = {
    "extreme": SeverityLevel.EXTREME,
    "severe": SeverityLevel.SEVERE,
    "moderate": SeverityLevel.MODERATE,
    "minor": SeverityLevel.LOW,
}


def get_cap_element(root: ET.Element, tag_name: str) -> ET.Element | None:
    """Finds an element by tag name regardless of XML namespace."""
    for el in root.iter():
        if el.tag.split("}")[-1] == tag_name:
            return el
    return None


def get_cap_text(root: ET.Element, tag_name: str, default: str = "") -> str:
    """Extracts text content for a tag regardless of XML namespace."""
    el = get_cap_element(root, tag_name)
    if el is not None and el.text:
        return el.text.strip()
    return default


def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    """Parses ISO-8601 timestamps found in CAP 1.2 XML."""
    if not dt_str or not dt_str.strip():
        return None
    try:
        dt = datetime.fromisoformat(dt_str.strip())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def extract_wind_speeds(text: str) -> tuple[Decimal | None, str | None, Decimal | None, str | None]:
    """
    Extracts sustained wind speed and gust speed from descriptive alert text.
    Returns (sustained_speed, sustained_unit, gust_speed, gust_unit).
    """
    sustained_val: Decimal | None = None
    sustained_txt: str | None = None
    gust_val: Decimal | None = None
    gust_txt: str | None = None

    # First look for gust speed anywhere: e.g. "gusting to 110 Kmph", "gust 80 kmph"
    gust_match = re.search(
        r'gust(?:ing)?(?:\s+to|\s+speed)?\s*[:=]?\s*(\d+)\s*(kmph|km/h|knots|kt)?',
        text,
        re.IGNORECASE,
    )
    unit_fallback = "kmph"
    if gust_match:
        val_str = gust_match.group(1)
        unit = gust_match.group(2).lower() if gust_match.group(2) else "kmph"
        unit_fallback = unit
        gust_val = Decimal(val_str)
        gust_txt = f"{val_str} {unit}"

    # Now look for sustained wind speed
    sustained_match = re.search(
        r'(?:maximum\s+)?(?:gale\s+|surface\s+|squally\s+)?wind(?:\s+speed)?\s*(?:of\s+|reaching\s+)?(\d+(?:-\d+)?)\s*(?:(kmph|km/h|knots|kt)|\s*(?=gust))?',
        text,
        re.IGNORECASE,
    )
    if sustained_match:
        val_str = sustained_match.group(1)
        matched_unit = sustained_match.group(2)
        unit = matched_unit.lower() if matched_unit else unit_fallback
        if "-" in val_str:
            parts = val_str.split("-")
            sustained_val = Decimal(parts[1])
        else:
            sustained_val = Decimal(val_str)
        sustained_txt = f"{val_str} {unit}"

    return sustained_val, sustained_txt, gust_val, gust_txt


def extract_center_coordinates(text: str) -> tuple[float, float] | None:
    """
    Extracts cyclone center coordinates from descriptive bulletin text if present.
    e.g. "near Latitude 14.5N and Longitude 85.3E" -> (85.3, 14.5)
    """
    coord_match = re.search(
        r'Latitude\s*(\d+(?:\.\d+)?)\s*([NSns])\s*(?:and)?\s*Longitude\s*(\d+(?:\.\d+)?)\s*([EWew])',
        text,
        re.IGNORECASE,
    )
    if coord_match:
        lat = float(coord_match.group(1))
        if coord_match.group(2).upper() == "S":
            lat = -lat
        lon = float(coord_match.group(3))
        if coord_match.group(4).upper() == "W":
            lon = -lon
        if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            return lon, lat
    return None


def extract_wave_height(text: str) -> tuple[Decimal | None, str | None]:
    """
    Extracts wave height from Telugu or English coastal alerts.
    e.g. "1.1-1.3 మీటర్ల ఎత్తులో అలల" or "waves of 1.4-1.6 meters height"
    """
    match = re.search(r'(\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)\s*(?:మీటర్ల|meters|m)', text, re.IGNORECASE)
    if match:
        val_str = match.group(1)
        if "-" in val_str:
            upper = val_str.split("-")[1]
            return Decimal(upper), f"{val_str} m"
        return Decimal(val_str), f"{val_str} m"
    return None, None


def parse_cap_polygon(polygon_text: str) -> MultiPolygon | None:
    """Parses whitespace-separated 'lat,lon' pairs into a PostGIS MultiPolygon."""
    if not polygon_text or not polygon_text.strip():
        return None
    try:
        coords: list[tuple[float, float]] = []
        for pair in polygon_text.strip().split():
            parts = pair.split(",")
            if len(parts) == 2:
                lat, lon = float(parts[0]), float(parts[1])
                coords.append((lon, lat))  # Shapely expects (x=lon, y=lat)
        if len(coords) < 3:
            return None
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if isinstance(poly, Polygon) and not poly.is_empty:
            return MultiPolygon([poly])
        elif isinstance(poly, MultiPolygon) and not poly.is_empty:
            return poly
        return None
    except Exception as exc:
        logger.debug(f"Failed to parse CAP polygon: {exc}")
        return None


def fetch_sachet_xml(url: str, timeout_seconds: float = 12.0) -> bytes:
    """Fetches raw XML bytes from SACHET."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)",
            "Accept": "application/xml, text/xml",
        },
    )

    with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as resp:
        return resp.read()


def sync_sachet_cyclone_alerts(
    session: Session,
    rss_xml_content: bytes | None = None,
    cap_xml_map: dict[str, bytes] | None = None,
    max_alerts: int = 15,
) -> SourceSyncSummary:
    """
    Adapter for NDMA SACHET Common Alerting Protocol (CAP) Cyclone & Coastal Hazard Alerts.

    - Reads the live All-India RSS feed from SACHET.
    - Filters items related to cyclones, gale winds, high waves, storm surges.
    - Retrieves CAP 1.2 XML documents.
    - Extracts sustained wind speed, gust speed, center coordinates, wave heights.
    - Normalizes into CrisisEvent, CrisisSourceReport, and HazardObservation.
    - Enforces idempotency via external_event_id + payload_hash.
    """
    now = datetime.now(timezone.utc)

    # 1. Ensure DataSource entry exists in DB
    sync_data_sources(session)
    stmt = select(DataSource).where(DataSource.code == "NDMA_SACHET")
    data_source = session.scalar(stmt)
    if data_source is None:
        return SourceSyncSummary(
            status="ERROR",
            message="Data source 'NDMA_SACHET' not found in registry.",
            errors=["DataSource NDMA_SACHET missing"],
        )

    data_source.last_attempt_at = now

    # 2. Fetch RSS if not injected
    if rss_xml_content is None:
        try:
            rss_xml_content = fetch_sachet_xml(SACHET_ALL_INDIA_RSS_URL)
        except urllib.error.HTTPError as exc:
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=f"SACHET RSS returned HTTP Error {exc.code}: {exc.reason}",
                errors=[str(exc)],
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=f"Failed to connect to NDMA SACHET RSS feed: {exc}",
                errors=[str(exc)],
            )
        except Exception as exc:
            session.commit()
            return SourceSyncSummary(
                status="ERROR",
                message=f"Unexpected error fetching SACHET RSS: {exc}",
                errors=[str(exc)],
            )

    # 3. Parse RSS items
    try:
        rss_root = ET.fromstring(rss_xml_content)
    except ET.ParseError as exc:
        session.commit()
        return SourceSyncSummary(
            status="ERROR",
            message=f"Failed to parse SACHET RSS XML: {exc}",
            errors=[str(exc)],
        )

    items = rss_root.findall(".//item")
    if not items:
        data_source.last_successful_fetch = now
        session.commit()
        return SourceSyncSummary(
            status="ACTIVE",
            fetched=0,
            created=0,
            updated=0,
            skipped=0,
            failed=0,
            message="No alerts currently active in SACHET RSS feed.",
        )

    fetched = 0
    created = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for item in items:
        if fetched >= max_alerts:
            break

        title = item.findtext("title", "")
        desc = item.findtext("description", "")
        link = item.findtext("link", "")

        combined_text = f"{title} {desc}".lower()
        if not any(k in combined_text for k in CYCLONE_KEYWORDS):
            continue

        fetched += 1
        ident = ""
        if "identifier=" in link:
            ident = link.split("identifier=")[-1]
        elif item.findtext("guid"):
            ident = item.findtext("guid", "").strip()

        if not ident:
            skipped += 1
            continue

        # 4. Fetch CAP 1.2 XML for this alert
        cap_bytes: bytes | None = None
        if cap_xml_map and ident in cap_xml_map:
            cap_bytes = cap_xml_map[ident]
        else:
            try:
                alert_url = f"{SACHET_XML_ALERT_BASE}{ident}"
                cap_bytes = fetch_sachet_xml(alert_url)
            except Exception as exc:
                logger.warning(f"Failed to fetch CAP XML for alert {ident}: {exc}")
                failed += 1
                errors.append(f"Failed to fetch CAP XML for {ident}: {exc}")
                continue

        # 5. Parse CAP XML
        try:
            cap_root = ET.fromstring(cap_bytes)
            identifier = get_cap_text(cap_root, "identifier", default=ident)
            sender = get_cap_text(cap_root, "sender", default="NDMA")
            sent_str = get_cap_text(cap_root, "sent")
            event = get_cap_text(cap_root, "event", default="Cyclone Warning")
            urgency = get_cap_text(cap_root, "urgency", default="Immediate")
            severity_raw = get_cap_text(cap_root, "severity", default="Moderate")
            headline = get_cap_text(cap_root, "headline", default=title)
            description = get_cap_text(cap_root, "description", default=desc)
            instruction = get_cap_text(cap_root, "instruction", default="")
            effective_str = get_cap_text(cap_root, "effective")
            expires_str = get_cap_text(cap_root, "expires")
            area_desc = get_cap_text(cap_root, "areaDesc", default="Coastal / Cyclone Affected Region")
            polygon_str = get_cap_text(cap_root, "polygon")

            started_at = parse_iso_datetime(effective_str) or parse_iso_datetime(sent_str) or now
            expires_at = parse_iso_datetime(expires_str)

            # Determine alert status
            status = CrisisStatus.ACTIVE
            if expires_at and expires_at < now:
                status = CrisisStatus.EXPIRED

            # Severity mapping
            severity = SEVERITY_MAPPING.get(severity_raw.lower(), SeverityLevel.MODERATE)

            # Extract meteorological observations
            full_text = f"{headline} {description}"
            sustained_val, sustained_unit, gust_val, gust_unit = extract_wind_speeds(full_text)
            center_coords = extract_center_coordinates(full_text)
            wave_val, wave_unit = extract_wave_height(full_text)

            # Geometries
            multi_poly = parse_cap_polygon(polygon_str) if polygon_str else None
            affected_geom = geojson_to_postgis(mapping(multi_poly)) if multi_poly else None

            location_geom = None
            if center_coords:
                location_geom = geojson_to_postgis({
                    "type": "Point",
                    "coordinates": [center_coords[0], center_coords[1]],
                })
            elif multi_poly:
                centroid = multi_poly.centroid
                location_geom = geojson_to_postgis({
                    "type": "Point",
                    "coordinates": [centroid.x, centroid.y],
                })

            # Canonical payload
            canonical_payload = {
                "identifier": identifier,
                "sender": sender,
                "event": event,
                "urgency": urgency,
                "severity": severity_raw,
                "headline": headline,
                "description": description,
                "instruction": instruction,
                "area_desc": area_desc,
                "sent": sent_str,
                "effective": effective_str,
                "expires": expires_str,
                "center_coordinates": center_coords,
                "extracted_observations": {
                    "sustained_wind_speed": str(sustained_val) if sustained_val else None,
                    "gust_speed": str(gust_val) if gust_val else None,
                    "wave_height": str(wave_val) if wave_val else None,
                },
            }
            payload_str = json.dumps(canonical_payload, sort_keys=True, default=str)
            payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            # Check if source report already exists
            report_stmt = select(CrisisSourceReport).where(
                CrisisSourceReport.data_source_id == data_source.id,
                CrisisSourceReport.external_event_id == identifier,
            )
            existing_report = session.scalar(report_stmt)

            if existing_report is not None:
                crisis_event = existing_report.crisis_event
                if existing_report.payload_hash == payload_hash:
                    crisis_event.last_seen_at = now
                    skipped += 1
                else:
                    existing_report.source_title = headline
                    existing_report.source_description = description
                    existing_report.source_severity = severity_raw
                    existing_report.source_status = status.value
                    existing_report.raw_payload = canonical_payload
                    existing_report.payload_hash = payload_hash
                    existing_report.fetched_at = now

                    crisis_event.title = headline[:500]
                    crisis_event.description = description
                    crisis_event.severity = severity
                    crisis_event.status = status
                    if location_geom:
                        crisis_event.location = location_geom
                    if affected_geom:
                        crisis_event.affected_area = affected_geom
                    crisis_event.last_seen_at = now
                    updated += 1
            else:
                # Create new CrisisEvent
                crisis_event = CrisisEvent(
                    id=uuid.uuid4(),
                    crisis_type=CrisisType.CYCLONE,
                    title=headline[:500],
                    description=description,
                    severity=severity,
                    status=status,
                    location=location_geom,
                    affected_area=affected_geom,
                    started_at=started_at,
                    expires_at=expires_at,
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
                    external_event_id=identifier,
                    source_url=f"{SACHET_XML_ALERT_BASE}{identifier}",
                    source_title=headline[:500],
                    source_description=description,
                    source_severity=severity_raw,
                    source_status=status.value,
                    issued_at=started_at,
                    valid_until=expires_at,
                    fetched_at=now,
                    raw_payload=canonical_payload,
                    payload_hash=payload_hash,
                )
                session.add(source_report)

                # Store sustained wind speed observation if detected
                if sustained_val is not None:
                    wind_obs = HazardObservation(
                        id=uuid.uuid4(),
                        crisis_event_id=crisis_event.id,
                        data_source_id=data_source.id,
                        observation_type="CYCLONE_MAX_WIND_SPEED",
                        value_numeric=sustained_val,
                        value_text=sustained_unit,
                        unit="kmph",
                        geometry=location_geom,
                        observed_at=started_at,
                        ingested_at=now,
                        properties={"event": event, "raw_text": headline[:200]},
                    )
                    session.add(wind_obs)

                # Store gust speed observation if detected
                if gust_val is not None:
                    gust_obs = HazardObservation(
                        id=uuid.uuid4(),
                        crisis_event_id=crisis_event.id,
                        data_source_id=data_source.id,
                        observation_type="CYCLONE_GUST_SPEED",
                        value_numeric=gust_val,
                        value_text=gust_unit,
                        unit="kmph",
                        geometry=location_geom,
                        observed_at=started_at,
                        ingested_at=now,
                        properties={"event": event, "raw_text": headline[:200]},
                    )
                    session.add(gust_obs)

                # Store wave height observation if detected
                if wave_val is not None:
                    wave_obs = HazardObservation(
                        id=uuid.uuid4(),
                        crisis_event_id=crisis_event.id,
                        data_source_id=data_source.id,
                        observation_type="COASTAL_WAVE_HEIGHT",
                        value_numeric=wave_val,
                        value_text=wave_unit,
                        unit="m",
                        geometry=location_geom,
                        observed_at=started_at,
                        ingested_at=now,
                        properties={"event": event, "raw_text": headline[:200]},
                    )
                    session.add(wave_obs)

                created += 1

        except Exception as exc:
            logger.error(f"Failed to process SACHET cyclone alert: {exc}", exc_info=True)
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
        message=f"SACHET cyclone ingestion completed: {created} created, {updated} updated, {skipped} skipped.",
        errors=errors,
    )
