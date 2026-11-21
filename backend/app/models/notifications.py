from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.enums import (
    AlertClassification,
    AlertSourceKind,
    CrisisType,
    enum_type,
)


class DeviceAlertProfile(Base):
    """
    Anonymous client device notification preferences and push registration.
    Keyed by unique installation/device ID.
    Does not require user authentication.
    """

    __tablename__ = "device_alert_profiles"
    __table_args__ = (
        CheckConstraint("length(trim(device_id)) > 0", name="device_id_nonempty"),
        CheckConstraint("notification_radius_km > 0", name="radius_positive"),
        Index("ix_device_alert_profiles_device_id", "device_id", unique=True),
        Index("ix_device_alert_profiles_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    device_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
    )
    push_token: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    alert_earthquake: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    alert_flood: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    alert_cyclone: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    alert_critical: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    alert_advisory: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    notification_radius_km: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        nullable=False,
        default=Decimal("50.0"),
        server_default="50.0",
    )
    sound_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    last_known_location: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    notifications: Mapped[list[AlertNotificationRecord]] = relationship(
        back_populates="device_profile",
        passive_deletes="all",
        primaryjoin="DeviceAlertProfile.device_id == foreign(AlertNotificationRecord.device_id)",
    )


class AlertNotificationRecord(Base):
    """
    Persistent log of disaster notifications generated and dispatched for a device.
    Supports in-app history, unread badge counts, and delivery audit.
    """

    __tablename__ = "alert_notifications"
    __table_args__ = (
        Index("ix_alert_notifications_device_id", "device_id"),
        Index("ix_alert_notifications_created_at", "created_at"),
        Index("ix_alert_notifications_is_read", "is_read"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    device_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    crisis_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crisis_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    crisis_source_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crisis_source_reports.id", ondelete="SET NULL"),
        nullable=True,
    )
    disaster_type: Mapped[CrisisType] = mapped_column(
        enum_type(CrisisType, "crisis_type"),
        nullable=False,
    )
    alert_classification: Mapped[AlertClassification] = mapped_column(
        enum_type(AlertClassification, "alert_classification"),
        nullable=False,
    )
    alert_source_kind: Mapped[AlertSourceKind] = mapped_column(
        enum_type(AlertSourceKind, "alert_source_kind"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    original_severity: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    issuing_authority: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    source_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
    )
    deep_link_url: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    delivery_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="SIMULATED",
        server_default="SIMULATED",
    )
    delivery_detail: Mapped[Any | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    device_profile: Mapped[DeviceAlertProfile | None] = relationship(
        back_populates="notifications",
        primaryjoin="DeviceAlertProfile.device_id == foreign(AlertNotificationRecord.device_id)",
    )


class AlertSyncLog(Base):
    """
    Audit log of automated background and manual synchronization runs.
    Records monitoring health, rate-limit awareness, and failure tracking.
    """

    __tablename__ = "alert_sync_logs"
    __table_args__ = (
        Index("ix_alert_sync_logs_source_code", "source_code"),
        Index("ix_alert_sync_logs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    source_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    sync_mode: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="SCHEDULED",
        server_default="SCHEDULED",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="SUCCESS",
        server_default="SUCCESS",
    )
    events_detected: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    alerts_generated: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


__all__ = [
    "AlertNotificationRecord",
    "AlertSyncLog",
    "DeviceAlertProfile",
]
