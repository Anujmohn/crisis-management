from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.models.enums import AlertClassification, AlertSourceKind, CrisisType
from app.schemas.common import GeoJSONGeometry, SchemaModel
from app.schemas.disaster_search import UserLocation


class AlertItem(SchemaModel):
    alert_id: UUID
    crisis_event_id: UUID | None = None
    crisis_source_report_id: UUID | None = None
    disaster_type: CrisisType
    alert_classification: AlertClassification
    alert_source_kind: AlertSourceKind
    title: str
    body: str
    original_severity: str | None = None
    issuing_authority: str | None = None
    source_url: str | None = None
    issued_at: datetime | None = None
    valid_until: datetime | None = None
    distance_km: float | None = None
    location: GeoJSONGeometry | None = None
    affected_area: GeoJSONGeometry | None = None
    freshness: str = "UNKNOWN"
    is_official_warning: bool = False
    deep_link_url: str | None = None


class NearbyAlertsRequest(SchemaModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(default=50.0, gt=0.0, le=1000.0)
    disaster_type: CrisisType | None = None
    classification: AlertClassification | None = None
    limit: int = Field(default=50, ge=1, le=200)


class NearbyAlertsResponse(SchemaModel):
    success: bool = True
    total_alerts: int = 0
    alerts: list[AlertItem] = Field(default_factory=list)
    search_center: UserLocation
    search_radius_km: float
    safety_disclaimer: str


class DevicePreferencesRead(SchemaModel):
    device_id: str
    push_token: str | None = None
    platform: str
    alert_earthquake: bool
    alert_flood: bool
    alert_cyclone: bool
    alert_critical: bool
    alert_advisory: bool
    notification_radius_km: float
    sound_enabled: bool
    is_active: bool
    updated_at: datetime


class DevicePreferencesUpdate(SchemaModel):
    device_id: str
    alert_earthquake: bool | None = None
    alert_flood: bool | None = None
    alert_cyclone: bool | None = None
    alert_critical: bool | None = None
    alert_advisory: bool | None = None
    notification_radius_km: float | None = Field(default=None, gt=0.0, le=500.0)
    sound_enabled: bool | None = None


class DeviceRegisterRequest(SchemaModel):
    device_id: str
    push_token: str | None = None
    platform: str = "unknown"
    alert_earthquake: bool = True
    alert_flood: bool = True
    alert_cyclone: bool = True
    alert_critical: bool = True
    alert_advisory: bool = True
    notification_radius_km: float = Field(default=50.0, gt=0.0, le=500.0)
    sound_enabled: bool = True
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)


class DeviceRegisterResponse(SchemaModel):
    success: bool = True
    device_id: str
    message: str
    preferences: DevicePreferencesRead


class AlertSyncRequest(SchemaModel):
    disaster_type: CrisisType | None = None
    source_code: str | None = None
    sync_mode: str = "MANUAL"


class AlertSyncResponse(SchemaModel):
    success: bool = True
    sync_mode: str = "MANUAL"
    total_events_detected: int = 0
    total_alerts_generated: int = 0
    sources_synced: list[str] = Field(default_factory=list)
    monitoring_health: str = "HEALTHY"
    message: str
    disclaimer: str


class NotificationHistoryItem(SchemaModel):
    notification_id: UUID
    device_id: str
    crisis_event_id: UUID | None = None
    disaster_type: CrisisType
    alert_classification: AlertClassification
    alert_source_kind: AlertSourceKind
    title: str
    body: str
    original_severity: str | None = None
    issuing_authority: str | None = None
    source_url: str | None = None
    deep_link_url: str | None = None
    delivery_status: str
    is_read: bool
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime


class NotificationHistoryResponse(SchemaModel):
    success: bool = True
    device_id: str
    total_notifications: int = 0
    unread_count: int = 0
    notifications: list[NotificationHistoryItem] = Field(default_factory=list)
    safety_disclaimer: str


__all__ = [
    "AlertItem",
    "AlertSyncRequest",
    "AlertSyncResponse",
    "DevicePreferencesRead",
    "DevicePreferencesUpdate",
    "DeviceRegisterRequest",
    "DeviceRegisterResponse",
    "NearbyAlertsRequest",
    "NearbyAlertsResponse",
    "NotificationHistoryItem",
    "NotificationHistoryResponse",
]
