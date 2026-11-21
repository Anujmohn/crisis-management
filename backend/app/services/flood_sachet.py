from __future__ import annotations

import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import CrisisEvent, CrisisSourceReport, DataSource
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

SACHET_ALL_INDIA_RSS_URL = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"
SACHET_XML_ALERT_BASE = "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier="

# Keywords that indicate flood / inundation / precipitation hazards
FLOOD_KEYWORDS = ("rain", "flood", "inundat", "waterlog", "water level", "downpour")

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
    """
    Parses ISO-8601 timestamps commonly found in CAP 1.2 XML.
    """
    if not dt_str or not dt_str.strip():
        return None
    try:
        dt = datetime.fromisoformat(dt_str.strip())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_sachet_xml(url: str, timeout_seconds: float = 12.0) -> bytes:
    """
    Fetches raw XML bytes from SACHET.
    """
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


def sync_sachet_flood_alerts(
    session: Session,
    rss_xml_content: bytes | None = None,
    max_alerts: int = 15,
) -> SourceSyncSummary:
    """
    Adapter for NDMA SACHET Common Alerting Protocol (CAP) Flood & Heavy Rain Alerts.

    - Reads the live All-India RSS feed from SACHET.
    - Filters items related to floods, heavy rain, and waterlogging.
    - Retrieves the CAP 1.2 XML document for each alert.
    - Normalizes into CrisisEvent and CrisisSourceReport.
    - Idempotently prevents duplicate alerts using the unique CAP identifier.
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

    # 2. Fetch RSS feed if not provided
    if rss_xml_content is None:
        try:
            rss_xml_content = fetch_sachet_xml(SACHET_ALL_INDIA_RSS_URL)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error_msg = f"Network failure fetching SACHET RSS feed: {exc}"
            logger.error(error_msg)
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=error_msg,
                errors=[str(exc)],
            )

    # 3. Parse RSS XML items
    try:
        rss_root = ET.fromstring(rss_xml_content)
    except ET.ParseError as exc:
        return SourceSyncSummary(
            status="ERROR",
            message=f"Invalid XML in SACHET RSS feed: {exc}",
            errors=[str(exc)],
        )

    items = rss_root.findall(".//item")
    flood_items = []
    for item in items:
        title = item.findtext("title", "").lower()
        cat = item.findtext("category", "")
        if any(kw in title for kw in FLOOD_KEYWORDS) or cat == "Met":
            flood_items.append(item)

    if max_alerts is not None:
        flood_items = flood_items[:max_alerts]

    fetched_count = len(flood_items)
    created_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    # 4. Ingest each flood alert
    for item in flood_items:
        try:
            link = item.findtext("link", "").strip()
            if not link:
                continue

            # Extract identifier parameter from link
            id_idx = link.find("identifier=")
            identifier = link[id_idx + 11:] if id_idx != -1 else None
            if not identifier:
                continue

            # Check if source report already exists
            report_stmt = select(CrisisSourceReport).where(
                CrisisSourceReport.data_source_id == data_source.id,
                CrisisSourceReport.external_event_id == identifier,
            )
            existing_report = session.scalar(report_stmt)

            # If existing and recent, skip refetching CAP XML
            if existing_report is not None:
                existing_event = session.get(CrisisEvent, existing_report.crisis_event_id)
                if existing_event is not None:
                    existing_event.last_seen_at = now
                skipped_count += 1
                continue

            # Fetch detailed CAP 1.2 XML
            alert_xml = fetch_sachet_xml(f"{SACHET_XML_ALERT_BASE}{identifier}")
            alert_root = ET.fromstring(alert_xml)

            # Extract CAP fields ignoring namespaces
            sender = get_cap_text(alert_root, "sender", "NDMA")
            sent_str = get_cap_text(alert_root, "sent")
            sent_at = parse_iso_datetime(sent_str) or now
            status_str = get_cap_text(alert_root, "status", "Actual")

            info_el = get_cap_element(alert_root, "info")
            if info_el is None:
                continue

            event_name = get_cap_text(info_el, "event", "Flood Alert")
            severity_str = get_cap_text(info_el, "severity", "Moderate").lower()
            severity = SEVERITY_MAPPING.get(severity_str, SeverityLevel.MODERATE)

            effective_at = parse_iso_datetime(get_cap_text(info_el, "effective")) or sent_at
            expires_at = parse_iso_datetime(get_cap_text(info_el, "expires")) or (effective_at + timedelta(hours=24))

            headline = get_cap_text(info_el, "headline") or event_name
            description = get_cap_text(info_el, "description")
            instruction = get_cap_text(info_el, "instruction")
            area_desc = get_cap_text(info_el, "areaDesc", "India")

            full_desc = (
                f"{headline}\n\n"
                f"Area Affected: {area_desc}\n"
                f"Instructions: {instruction}\n"
                f"Issuing Agency: {sender}\n"
                f"Effective: {effective_at.isoformat()} to {expires_at.isoformat()}"
            )

            is_active = now <= expires_at
            event_status = CrisisStatus.ACTIVE if is_active else CrisisStatus.EXPIRED

            payload_dict = {
                "identifier": identifier,
                "sender": sender,
                "sent": sent_at.isoformat(),
                "event": event_name,
                "severity": severity_str,
                "headline": headline,
                "areaDesc": area_desc,
                "instruction": instruction,
                "effective": effective_at.isoformat(),
                "expires": expires_at.isoformat(),
            }
            payload_str = json.dumps(payload_dict, sort_keys=True)
            payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            # Create CrisisEvent
            crisis_event = CrisisEvent(
                id=uuid.uuid4(),
                crisis_type=CrisisType.FLOOD,
                title=f"Flood Warning: {headline[:400]}",
                description=full_desc,
                severity=severity,
                status=event_status,
                location=None,
                affected_area=None,
                started_at=effective_at,
                expires_at=expires_at,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(crisis_event)

            # Create CrisisSourceReport
            source_report = CrisisSourceReport(
                id=uuid.uuid4(),
                crisis_event=crisis_event,
                data_source=data_source,
                external_event_id=identifier,
                source_url=link,
                source_title=headline[:500],
                source_description=full_desc,
                source_severity=severity_str.title(),
                source_status=status_str,
                issued_at=sent_at,
                valid_until=expires_at,
                fetched_at=now,
                raw_payload=payload_dict,
                payload_hash=payload_hash,
            )
            session.add(source_report)
            created_count += 1

        except Exception as exc:
            logger.exception("Error ingesting SACHET flood alert: %s", exc)
            failed_count += 1
            errors.append(str(exc))

    data_source.last_successful_fetch = now
    session.commit()

    return SourceSyncSummary(
        status="ACTIVE",
        fetched=fetched_count,
        created=created_count,
        updated=updated_count,
        skipped=skipped_count,
        failed=failed_count,
        message=f"Synchronized {fetched_count} SACHET flood alerts.",
        errors=errors[:10],
    )
