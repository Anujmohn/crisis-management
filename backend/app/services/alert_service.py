from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database.geometry import geojson_to_postgis, postgis_to_geojson
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource
from app.models.enums import (
    AlertClassification,
    AlertSourceKind,
    CrisisStatus,
    CrisisType,
    SeverityLevel,
)
from app.models.notifications import (
    AlertNotificationRecord,
    AlertSyncLog,
    DeviceAlertProfile,
)
from app.schemas.alert import (
    AlertItem,
    AlertSyncResponse,
    DevicePreferencesRead,
    DevicePreferencesUpdate,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    NearbyAlertsResponse,
    NotificationHistoryItem,
    NotificationHistoryResponse,
)
from app.schemas.disaster_search import UserLocation
from app.services.cyclone_ingestion import sync_all_cyclone_sources
from app.services.disaster_search import evaluate_freshness, safe_postgis_to_geojson
from app.services.earthquake_ingestion import sync_earthquakes_from_ncs
from app.services.flood_ingestion import sync_all_flood_sources

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

ALERT_SAFETY_DISCLAIMER = (
    "Disaster alerts and notifications are compiled from verified official feeds and spatial matching. "
    "Absence of an active alert or delivery failure does NOT guarantee absence of danger. "
    "Always observe real-time environmental conditions and follow official directives from NDMA, SDMA, and local emergency authorities."
)


def _is_expo_token(token: str | None) -> bool:
    """Checks whether the token follows the Expo push token format."""
    if not token or not isinstance(token, str):
        return False
    return token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")


def classify_report_and_event(
    report: CrisisSourceReport,
    event: CrisisEvent,
    is_new: bool = False,
    is_updated: bool = False,
    previous_severity: str | None = None,
) -> tuple[AlertClassification, AlertSourceKind]:
    """
    Classifies an alert based on source authority, reporting status, and update type.
    Strictly distinguishes official government warnings from observations and technical notifications.
    """
    now = datetime.now(timezone.utc)
    source_code = report.data_source.code if report.data_source else ""

    # Expired check
    if report.valid_until and report.valid_until < now:
        return AlertClassification.WARNING_EXPIRED, AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING

    # Cancelled check
    if (report.source_status and "cancel" in report.source_status.lower()) or event.status == CrisisStatus.CANCELLED:
        return AlertClassification.WARNING_CANCELLED, AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING

    # Official Government Warning Sources
    is_official_warning_source = source_code in ("NDMA_SACHET", "IMD_WEATHER_API", "CWC_FLOOD")

    if is_official_warning_source:
        source_kind = AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING

        # Escalation check
        if is_updated and previous_severity and report.source_severity:
            prev_s = str(previous_severity).upper()
            curr_s = str(report.source_severity).upper()
            if prev_s in ("LOW", "MODERATE", "YELLOW") and curr_s in ("HIGH", "SEVERE", "EXTREME", "ORANGE", "RED"):
                return AlertClassification.WARNING_ESCALATION, source_kind

        if is_updated:
            return AlertClassification.WARNING_UPDATE, source_kind

        # Check if Advisory vs Warning
        title_desc = f"{report.source_title or ''} {report.source_description or ''}".lower()
        if "advisory" in title_desc or (report.source_severity and report.source_severity.lower() in ("minor", "low", "yellow")):
            return AlertClassification.OFFICIAL_ADVISORY, source_kind

        return AlertClassification.OFFICIAL_WARNING, source_kind

    # Observation Sources (e.g. NCS Earthquake observations, INCOIS wave measurements)
    if source_code in ("NCS_SEISMO", "INCOIS_OCEAN"):
        source_kind = AlertSourceKind.OFFICIAL_OBSERVATION
        if is_updated:
            return AlertClassification.SIGNIFICANT_HAZARD_CHANGE, source_kind
        return AlertClassification.NEW_DISASTER_EVENT, source_kind

    # Fallback / Technical
    return AlertClassification.NEW_DISASTER_EVENT, AlertSourceKind.OFFICIAL_OBSERVATION


def get_nearby_alerts(
    session: Session,
    latitude: float,
    longitude: float,
    radius_km: float = 50.0,
    disaster_type: CrisisType | None = None,
    classification: AlertClassification | None = None,
    limit: int = 50,
) -> NearbyAlertsResponse:
    """
    Retrieves active disaster alerts and official warnings relevant to a user's location.

    - Uses PostGIS spatial queries (ST_DWithin for point events, ST_Intersects for polygons).
    - Preserves source provenance, original severity, issuing authority, and validity dates.
    - Eliminates expired warnings from active presentation.
    - Does not convert raw earthquake magnitude directly into an official government warning.
    """
    now = datetime.now(timezone.utc)
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    user_geom = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    radius_meters = float(radius_km) * 1000.0

    # Distances
    dist_point = func.ST_Distance(CrisisEvent.location.cast(Geography), user_geog)
    dist_affected = func.ST_Distance(CrisisEvent.affected_area.cast(Geography), user_geog)

    # Spatial condition:
    # 1. Point location is within radius
    # 2. OR Affected area polygon intersects user point directly
    # 3. OR Affected area polygon boundary is within radius
    spatial_cond = or_(
        and_(CrisisEvent.location.is_not(None), func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, radius_meters)),
        and_(CrisisEvent.affected_area.is_not(None), func.ST_Intersects(CrisisEvent.affected_area, user_geom)),
        and_(CrisisEvent.affected_area.is_not(None), func.ST_DWithin(CrisisEvent.affected_area.cast(Geography), user_geog, radius_meters)),
    )

    query = (
        select(
            CrisisEvent,
            dist_point.label("dist_pt"),
            dist_affected.label("dist_aff"),
        )
        .options(
            selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
        .where(
            and_(
                CrisisEvent.status.in_([CrisisStatus.ACTIVE, CrisisStatus.UNKNOWN]),
                or_(
                    CrisisEvent.expires_at.is_(None),
                    CrisisEvent.expires_at >= now,
                ),
                spatial_cond,
            )
        )
    )

    if disaster_type is not None:
        query = query.where(CrisisEvent.crisis_type == disaster_type)

    query = query.order_by(CrisisEvent.started_at.desc().nullslast()).limit(limit)

    results = session.execute(query).all()

    alert_items: list[AlertItem] = []

    for row in results:
        event = row[0]
        d_pt = row[1]
        d_aff = row[2]

        min_dist_m: float | None = None
        if d_aff is not None and d_aff == 0.0:
            min_dist_m = 0.0
        elif d_pt is not None and d_aff is not None:
            min_dist_m = min(float(d_pt), float(d_aff))
        elif d_pt is not None:
            min_dist_m = float(d_pt)
        elif d_aff is not None:
            min_dist_m = float(d_aff)

        dist_km = round(min_dist_m / 1000.0, 2) if min_dist_m is not None else None
        freshness = evaluate_freshness(event, now)

        # For each source report associated with this event
        reports = event.source_reports or []
        if not reports:
            # Standalone event without source report
            c_cls = AlertClassification.NEW_DISASTER_EVENT
            s_kind = AlertSourceKind.OFFICIAL_OBSERVATION
            if classification and c_cls != classification:
                continue

            alert_items.append(
                AlertItem(
                    alert_id=uuid.uuid4(),
                    crisis_event_id=event.id,
                    crisis_source_report_id=None,
                    disaster_type=event.crisis_type,
                    alert_classification=c_cls,
                    alert_source_kind=s_kind,
                    title=event.title or f"{event.crisis_type.value} Event",
                    body=event.description or "Active disaster incident reported.",
                    original_severity=event.severity.value if event.severity else None,
                    issuing_authority="Official Emergency Network",
                    source_url=None,
                    issued_at=event.started_at or event.first_seen_at,
                    valid_until=event.expires_at,
                    distance_km=dist_km,
                    location=safe_postgis_to_geojson(event.location),
                    affected_area=safe_postgis_to_geojson(event.affected_area),
                    freshness=freshness,
                    is_official_warning=False,
                    deep_link_url=f"disasterapp://events/{event.id}",
                )
            )
        else:
            for rep in reports:
                c_cls, s_kind = classify_report_and_event(rep, event)
                if classification and c_cls != classification:
                    continue

                is_off_warn = s_kind == AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING
                authority = rep.data_source.name if rep.data_source else "Official Authority"

                alert_items.append(
                    AlertItem(
                        alert_id=rep.id,
                        crisis_event_id=event.id,
                        crisis_source_report_id=rep.id,
                        disaster_type=event.crisis_type,
                        alert_classification=c_cls,
                        alert_source_kind=s_kind,
                        title=rep.source_title or event.title or f"{event.crisis_type.value} Alert",
                        body=rep.source_description or event.description or "Official disaster report issued.",
                        original_severity=rep.source_severity or (event.severity.value if event.severity else None),
                        issuing_authority=authority,
                        source_url=rep.source_url,
                        issued_at=rep.issued_at or event.started_at,
                        valid_until=rep.valid_until or event.expires_at,
                        distance_km=dist_km,
                        location=safe_postgis_to_geojson(event.location),
                        affected_area=safe_postgis_to_geojson(event.affected_area),
                        freshness=freshness,
                        is_official_warning=is_off_warn,
                        deep_link_url=f"disasterapp://events/{event.id}",
                    )
                )

    return NearbyAlertsResponse(
        success=True,
        total_alerts=len(alert_items),
        alerts=alert_items,
        search_center=UserLocation(latitude=latitude, longitude=longitude),
        search_radius_km=radius_km,
        safety_disclaimer=ALERT_SAFETY_DISCLAIMER,
    )


# ---------------------------------------------------------------------------
# Device Preferences & Push Token Management
# ---------------------------------------------------------------------------


def register_or_update_device(
    session: Session,
    request: DeviceRegisterRequest,
) -> DeviceRegisterResponse:
    """
    Registers or updates an anonymous device profile with push token and preferences.
    Safe development-compatible approach without requiring complex user authentication.
    """
    now = datetime.now(timezone.utc)
    profile = session.scalars(
        select(DeviceAlertProfile).where(DeviceAlertProfile.device_id == request.device_id)
    ).first()

    loc_wkb = None
    if request.latitude is not None and request.longitude is not None:
        loc_wkb = geojson_to_postgis(
            {"type": "Point", "coordinates": [request.longitude, request.latitude]}
        )

    if profile is None:
        profile = DeviceAlertProfile(
            id=uuid.uuid4(),
            device_id=request.device_id,
            push_token=request.push_token,
            platform=request.platform,
            alert_earthquake=request.alert_earthquake,
            alert_flood=request.alert_flood,
            alert_cyclone=request.alert_cyclone,
            alert_critical=request.alert_critical,
            alert_advisory=request.alert_advisory,
            notification_radius_km=Decimal(str(request.notification_radius_km)),
            sound_enabled=request.sound_enabled,
            last_known_location=loc_wkb,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
        session.add(profile)
        msg = f"Device '{request.device_id}' registered successfully."
    else:
        profile.push_token = request.push_token or profile.push_token
        profile.platform = request.platform or profile.platform
        profile.alert_earthquake = request.alert_earthquake
        profile.alert_flood = request.alert_flood
        profile.alert_cyclone = request.alert_cyclone
        profile.alert_critical = request.alert_critical
        profile.alert_advisory = request.alert_advisory
        profile.notification_radius_km = Decimal(str(request.notification_radius_km))
        profile.sound_enabled = request.sound_enabled
        if loc_wkb is not None:
            profile.last_known_location = loc_wkb
        profile.is_active = True
        profile.last_active_at = now
        profile.updated_at = now
        msg = f"Device '{request.device_id}' preferences updated."

    session.commit()

    return DeviceRegisterResponse(
        success=True,
        device_id=profile.device_id,
        message=msg,
        preferences=DevicePreferencesRead(
            device_id=profile.device_id,
            push_token=profile.push_token,
            platform=profile.platform,
            alert_earthquake=profile.alert_earthquake,
            alert_flood=profile.alert_flood,
            alert_cyclone=profile.alert_cyclone,
            alert_critical=profile.alert_critical,
            alert_advisory=profile.alert_advisory,
            notification_radius_km=float(profile.notification_radius_km),
            sound_enabled=profile.sound_enabled,
            is_active=profile.is_active,
            updated_at=profile.updated_at,
        ),
    )


def get_device_preferences(
    session: Session,
    device_id: str,
) -> DevicePreferencesRead:
    """Retrieves alert preferences for a specific device installation."""
    profile = session.scalars(
        select(DeviceAlertProfile).where(DeviceAlertProfile.device_id == device_id)
    ).first()

    if profile is None:
        # Return sensible defaults if device has not yet registered
        return DevicePreferencesRead(
            device_id=device_id,
            push_token=None,
            platform="unknown",
            alert_earthquake=True,
            alert_flood=True,
            alert_cyclone=True,
            alert_critical=True,
            alert_advisory=True,
            notification_radius_km=50.0,
            sound_enabled=True,
            is_active=True,
            updated_at=datetime.now(timezone.utc),
        )

    return DevicePreferencesRead(
        device_id=profile.device_id,
        push_token=profile.push_token,
        platform=profile.platform,
        alert_earthquake=profile.alert_earthquake,
        alert_flood=profile.alert_flood,
        alert_cyclone=profile.alert_cyclone,
        alert_critical=profile.alert_critical,
        alert_advisory=profile.alert_advisory,
        notification_radius_km=float(profile.notification_radius_km),
        sound_enabled=profile.sound_enabled,
        is_active=profile.is_active,
        updated_at=profile.updated_at,
    )


def update_device_preferences(
    session: Session,
    update: DevicePreferencesUpdate,
) -> DevicePreferencesRead:
    """Updates selected alert preferences for a device."""
    profile = session.scalars(
        select(DeviceAlertProfile).where(DeviceAlertProfile.device_id == update.device_id)
    ).first()

    now = datetime.now(timezone.utc)
    if profile is None:
        profile = DeviceAlertProfile(
            id=uuid.uuid4(),
            device_id=update.device_id,
            push_token=None,
            platform="unknown",
            alert_earthquake=update.alert_earthquake if update.alert_earthquake is not None else True,
            alert_flood=update.alert_flood if update.alert_flood is not None else True,
            alert_cyclone=update.alert_cyclone if update.alert_cyclone is not None else True,
            alert_critical=update.alert_critical if update.alert_critical is not None else True,
            alert_advisory=update.alert_advisory if update.alert_advisory is not None else True,
            notification_radius_km=Decimal(str(update.notification_radius_km)) if update.notification_radius_km is not None else Decimal("50.0"),
            sound_enabled=update.sound_enabled if update.sound_enabled is not None else True,
            is_active=True,
            created_at=now,
            updated_at=now,
            last_active_at=now,
        )
        session.add(profile)
    else:
        if update.alert_earthquake is not None:
            profile.alert_earthquake = update.alert_earthquake
        if update.alert_flood is not None:
            profile.alert_flood = update.alert_flood
        if update.alert_cyclone is not None:
            profile.alert_cyclone = update.alert_cyclone
        if update.alert_critical is not None:
            profile.alert_critical = update.alert_critical
        if update.alert_advisory is not None:
            profile.alert_advisory = update.alert_advisory
        if update.notification_radius_km is not None:
            profile.notification_radius_km = Decimal(str(update.notification_radius_km))
        if update.sound_enabled is not None:
            profile.sound_enabled = update.sound_enabled
        profile.updated_at = now

    session.commit()

    return DevicePreferencesRead(
        device_id=profile.device_id,
        push_token=profile.push_token,
        platform=profile.platform,
        alert_earthquake=profile.alert_earthquake,
        alert_flood=profile.alert_flood,
        alert_cyclone=profile.alert_cyclone,
        alert_critical=profile.alert_critical,
        alert_advisory=profile.alert_advisory,
        notification_radius_km=float(profile.notification_radius_km),
        sound_enabled=profile.sound_enabled,
        is_active=profile.is_active,
        updated_at=profile.updated_at,
    )


def unregister_device(
    session: Session,
    device_id: str,
) -> bool:
    """Deactivates a device registration and clears its push token."""
    profile = session.scalars(
        select(DeviceAlertProfile).where(DeviceAlertProfile.device_id == device_id)
    ).first()

    if profile is None:
        return False

    profile.is_active = False
    profile.push_token = None
    profile.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Mobile Push Dispatching & In-App Notification History
# ---------------------------------------------------------------------------


def send_expo_push_notification(
    push_token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    sound: str = "default",
    timeout: float = 4.0,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Dispatches a push notification via the official Expo Push API.
    Handles network errors, token validation, and records delivery responses.
    """
    if not _is_expo_token(push_token):
        return False, "SIMULATED", {"reason": "Non-Expo token or simulated environment"}

    payload = {
        "to": push_token,
        "title": title,
        "body": body,
        "sound": sound,
        "priority": "high",
        "channelId": "disaster-alerts",
        "data": data or {},
    }

    try:
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            EXPO_PUSH_URL,
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            resp_json = json.loads(resp_body)
            return True, "DELIVERED", resp_json
    except urllib.error.HTTPError as http_err:
        err_msg = f"Expo push HTTP error: {http_err.code}"
        logger.warning(err_msg)
        return False, "FAILED", {"error": err_msg, "code": http_err.code}
    except Exception as exc:
        err_msg = f"Expo push transport error: {exc}"
        logger.warning(err_msg)
        return False, "FAILED", {"error": err_msg}


def dispatch_alert_notifications_for_report(
    session: Session,
    report: CrisisSourceReport,
    event: CrisisEvent,
    classification: AlertClassification,
    source_kind: AlertSourceKind,
) -> int:
    """
    Dispatches push notifications and records persistent in-app notifications
    for all active registered devices matching spatial and category preferences.
    """
    now = datetime.now(timezone.utc)
    title = f"[{report.source_severity or classification.value}] {report.source_title or event.title or 'Disaster Warning'}"
    body = report.source_description or event.description or "Official disaster warning issued for your region."
    issuing_authority = report.data_source.name if report.data_source else "Official Disaster Authority"
    deep_link = f"disasterapp://events/{event.id}"

    # Query active devices
    devices = session.scalars(
        select(DeviceAlertProfile).where(DeviceAlertProfile.is_active.is_(True))
    ).all()

    dispatched_count = 0

    for dev in devices:
        # 1. Check disaster category preferences
        if event.crisis_type == CrisisType.EARTHQUAKE and not dev.alert_earthquake:
            continue
        if event.crisis_type == CrisisType.FLOOD and not dev.alert_flood:
            continue
        if event.crisis_type == CrisisType.CYCLONE and not dev.alert_cyclone:
            continue

        # 2. Check warning vs advisory preferences
        if classification == AlertClassification.OFFICIAL_ADVISORY and not dev.alert_advisory:
            continue
        if classification in (AlertClassification.OFFICIAL_WARNING, AlertClassification.WARNING_ESCALATION) and not dev.alert_critical:
            continue

        # 3. Spatial relevance check:
        # If device has a last known location, verify spatial overlap
        if dev.last_known_location is not None:
            dev_geog = dev.last_known_location.cast(Geography)
            dev_geom = dev.last_known_location
            r_meters = float(dev.notification_radius_km) * 1000.0

            spatial_match = False
            # Check point location
            if event.location is not None:
                pt_dist_stmt = select(func.ST_DWithin(event.location.cast(Geography), dev_geog, r_meters))
                if session.scalar(pt_dist_stmt):
                    spatial_match = True

            # Check affected area polygon
            if not spatial_match and event.affected_area is not None:
                poly_stmt = select(
                    or_(
                        func.ST_Intersects(event.affected_area, dev_geom),
                        func.ST_DWithin(event.affected_area.cast(Geography), dev_geog, r_meters),
                    )
                )
                if session.scalar(poly_stmt):
                    spatial_match = True

            if not spatial_match:
                continue

        # 4. Deduplication: Check if notification already logged for this device & report
        existing_notif = session.scalars(
            select(AlertNotificationRecord).where(
                AlertNotificationRecord.device_id == dev.device_id,
                AlertNotificationRecord.crisis_source_report_id == report.id,
                AlertNotificationRecord.alert_classification == classification,
            )
        ).first()

        if existing_notif is not None:
            continue

        # 5. Dispatch via push service
        push_data = {
            "event_id": str(event.id),
            "report_id": str(report.id),
            "disaster_type": event.crisis_type.value,
            "classification": classification.value,
            "deep_link": deep_link,
        }

        delivery_status = "SIMULATED"
        delivery_resp = None

        if dev.push_token:
            sound_name = "default" if dev.sound_enabled else ""
            success, delivery_status, delivery_resp = send_expo_push_notification(
                push_token=dev.push_token,
                title=title,
                body=body,
                data=push_data,
                sound=sound_name,
            )

        # 6. Record in-app notification history
        notif_record = AlertNotificationRecord(
            id=uuid.uuid4(),
            device_id=dev.device_id,
            crisis_event_id=event.id,
            crisis_source_report_id=report.id,
            disaster_type=event.crisis_type,
            alert_classification=classification,
            alert_source_kind=source_kind,
            title=title,
            body=body,
            original_severity=report.source_severity,
            issuing_authority=issuing_authority,
            source_url=report.source_url,
            deep_link_url=deep_link,
            delivery_status=delivery_status,
            delivery_detail=delivery_resp,
            is_read=False,
            issued_at=report.issued_at,
            expires_at=report.valid_until,
            created_at=now,
        )
        session.add(notif_record)
        dispatched_count += 1

    session.commit()
    return dispatched_count


def get_device_notification_history(
    session: Session,
    device_id: str,
    limit: int = 50,
    unread_only: bool = False,
) -> NotificationHistoryResponse:
    """Retrieves in-app notification history and unread badge count for a device."""
    base_query = select(AlertNotificationRecord).where(AlertNotificationRecord.device_id == device_id)
    if unread_only:
        base_query = base_query.where(AlertNotificationRecord.is_read.is_(False))

    query = base_query.order_by(AlertNotificationRecord.created_at.desc()).limit(limit)
    records = list(session.scalars(query).all())

    # Total unread count
    unread_stmt = select(func.count()).select_from(AlertNotificationRecord).where(
        AlertNotificationRecord.device_id == device_id,
        AlertNotificationRecord.is_read.is_(False),
    )
    unread_count = session.scalar(unread_stmt) or 0

    items = [
        NotificationHistoryItem(
            notification_id=r.id,
            device_id=r.device_id,
            crisis_event_id=r.crisis_event_id,
            disaster_type=r.disaster_type,
            alert_classification=r.alert_classification,
            alert_source_kind=r.alert_source_kind,
            title=r.title,
            body=r.body,
            original_severity=r.original_severity,
            issuing_authority=r.issuing_authority,
            source_url=r.source_url,
            deep_link_url=r.deep_link_url,
            delivery_status=r.delivery_status,
            is_read=r.is_read,
            issued_at=r.issued_at,
            expires_at=r.expires_at,
            created_at=r.created_at,
        )
        for r in records
    ]

    return NotificationHistoryResponse(
        success=True,
        device_id=device_id,
        total_notifications=len(items),
        unread_count=unread_count,
        notifications=items,
        safety_disclaimer=ALERT_SAFETY_DISCLAIMER,
    )


def mark_notification_read(
    session: Session,
    notification_id: UUID,
    device_id: str | None = None,
) -> bool:
    """Marks a notification record as read by the user."""
    query = select(AlertNotificationRecord).where(AlertNotificationRecord.id == notification_id)
    if device_id:
        query = query.where(AlertNotificationRecord.device_id == device_id)

    record = session.scalars(query).first()
    if record is None:
        return False

    record.is_read = True
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Alert Synchronization & Background Monitoring
# ---------------------------------------------------------------------------


def sync_active_disaster_alerts(
    session: Session,
    disaster_type: CrisisType | None = None,
    source_code: str | None = None,
    sync_mode: str = "MANUAL",
) -> AlertSyncResponse:
    """
    Synchronizes verified official Indian disaster feeds and generates alerts.

    1. Executes verified ingestion adapters (NCS for Earthquakes, CWC/IMD/SACHET for Floods, IMD/INCOIS/SACHET for Cyclones).
    2. Identifies new and updated official reports.
    3. Performs spatial relevance evaluation and deduplication.
    4. Dispatches push notifications to registered devices.
    5. Records an audit log of monitoring health.
    """
    now = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    sources_synced: list[str] = []
    total_events = 0
    total_alerts = 0
    errors: list[str] = []

    # 1. Ingest Earthquake (NCS)
    if disaster_type is None or disaster_type == CrisisType.EARTHQUAKE:
        if source_code is None or source_code == "NCS_SEISMO":
            try:
                eq_res = sync_earthquakes_from_ncs(session)
                sources_synced.append("NCS_SEISMO")
                total_events += eq_res.created + eq_res.updated
            except Exception as exc:
                logger.warning("Error syncing NCS earthquakes: %s", exc)
                errors.append(f"NCS: {exc}")

    # 2. Ingest Flood (IMD, CWC, SACHET)
    if disaster_type is None or disaster_type == CrisisType.FLOOD:
        try:
            fl_res = sync_all_flood_sources(session)
            sources_synced.append("FLOOD_SOURCES")
            total_events += fl_res.total_created + fl_res.total_updated
        except Exception as exc:
            logger.warning("Error syncing flood sources: %s", exc)
            errors.append(f"Flood: {exc}")

    # 3. Ingest Cyclone (IMD, INCOIS, SACHET)
    if disaster_type is None or disaster_type == CrisisType.CYCLONE:
        try:
            cy_res = sync_all_cyclone_sources(session)
            sources_synced.append("CYCLONE_SOURCES")
            total_events += cy_res.total_created + cy_res.total_updated
        except Exception as exc:
            logger.warning("Error syncing cyclone sources: %s", exc)
            errors.append(f"Cyclone: {exc}")

    # 4. Check recent reports and trigger alert notifications for eligible devices
    recent_reports = session.scalars(
        select(CrisisSourceReport)
        .options(
            selectinload(CrisisSourceReport.crisis_event),
            selectinload(CrisisSourceReport.data_source),
        )
        .where(CrisisSourceReport.fetched_at >= now)
        .order_by(CrisisSourceReport.fetched_at.desc())
    ).all()

    for rep in recent_reports:
        ev = rep.crisis_event
        if ev and ev.status == CrisisStatus.ACTIVE:
            classification, source_kind = classify_report_and_event(rep, ev, is_new=True)
            sent = dispatch_alert_notifications_for_report(
                session=session,
                report=rep,
                event=ev,
                classification=classification,
                source_kind=source_kind,
            )
            total_alerts += sent

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    sync_status = "SUCCESS" if not errors else ("PARTIAL" if sources_synced else "FAILED")

    # Record Sync Log
    log_entry = AlertSyncLog(
        id=uuid.uuid4(),
        source_code=source_code or "ALL_OFFICIAL_SOURCES",
        sync_mode=sync_mode,
        status=sync_status,
        events_detected=total_events,
        alerts_generated=total_alerts,
        error_message="; ".join(errors) if errors else None,
        started_at=now,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(log_entry)
    session.commit()

    return AlertSyncResponse(
        success=sync_status != "FAILED",
        sync_mode=sync_mode,
        total_events_detected=total_events,
        total_alerts_generated=total_alerts,
        sources_synced=sources_synced,
        monitoring_health=sync_status,
        message=(
            f"Alert synchronization completed in {elapsed_ms}ms ({total_events} events detected, "
            f"{total_alerts} notifications dispatched across {len(sources_synced)} sources)."
        ),
        disclaimer=ALERT_SAFETY_DISCLAIMER,
    )


# ---------------------------------------------------------------------------
# Background Alert Monitoring Worker & Lifespan Hooks
# ---------------------------------------------------------------------------

_monitor_task: asyncio.Task[None] | None = None
_monitor_stop_event: asyncio.Event | None = None
_sync_lock: asyncio.Lock | None = None


def _get_sync_lock() -> asyncio.Lock:
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()
    return _sync_lock


async def background_monitoring_worker(interval_seconds: int = 300) -> None:
    """Lightweight background monitoring task for verified official disaster feeds."""
    logger.info("Background disaster alert monitoring task started (interval=%ds).", interval_seconds)
    sync_lock = _get_sync_lock()

    while _monitor_stop_event and not _monitor_stop_event.is_set():
        try:
            async with sync_lock:
                from app.database.session import SessionLocal

                with SessionLocal() as db_session:
                    sync_active_disaster_alerts(db_session, sync_mode="SCHEDULED")
        except Exception as exc:
            logger.warning("Background alert monitor synchronization error: %s", exc)

        try:
            await asyncio.wait_for(_monitor_stop_event.wait(), timeout=float(interval_seconds))
        except asyncio.TimeoutError:
            pass

    logger.info("Background disaster alert monitoring task stopped.")


def start_background_alert_monitor(interval_seconds: int = 300) -> None:
    """Initializes the background alert monitoring task if not already running."""
    global _monitor_task, _monitor_stop_event
    if _monitor_task is not None and not _monitor_task.done():
        return

    _monitor_stop_event = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        _monitor_task = loop.create_task(background_monitoring_worker(interval_seconds))
    except RuntimeError:
        logger.debug("No running asyncio event loop; background monitoring will start when app runs.")


def stop_background_alert_monitor() -> None:
    """Stops the background alert monitoring task gracefully."""
    global _monitor_task, _monitor_stop_event
    if _monitor_stop_event is not None:
        _monitor_stop_event.set()
    if _monitor_task is not None:
        _monitor_task.cancel()
        _monitor_task = None



__all__ = [
    "ALERT_SAFETY_DISCLAIMER",
    "classify_report_and_event",
    "dispatch_alert_notifications_for_report",
    "get_device_notification_history",
    "get_device_preferences",
    "get_nearby_alerts",
    "mark_notification_read",
    "register_or_update_device",
    "send_expo_push_notification",
    "sync_active_disaster_alerts",
    "unregister_device",
    "update_device_preferences",
]
