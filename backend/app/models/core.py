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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel, enum_type


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("length(trim(code)) > 0", name="code_nonempty"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("length(trim(source_type)) > 0", name="source_type_nonempty"),
        CheckConstraint(
            "update_interval_seconds >= 0",
            name="update_interval_nonnegative",
        ),
        Index("ix_data_sources_source_type", "source_type"),
        Index("ix_data_sources_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    update_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_successful_fetch: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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

    crisis_reports: Mapped[list[CrisisSourceReport]] = relationship(
        back_populates="data_source",
        passive_deletes="all",
    )
    hazard_observations: Mapped[list[HazardObservation]] = relationship(
        back_populates="data_source",
        passive_deletes="all",
    )
    safe_location_references: Mapped[list[SafeLocationSourceReference]] = relationship(
        back_populates="data_source",
        passive_deletes="all",
    )


class CrisisEvent(Base):
    __tablename__ = "crisis_events"
    __table_args__ = (
        CheckConstraint(
            "expires_at IS NULL OR started_at IS NULL OR expires_at >= started_at",
            name="expires_after_start",
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="last_seen_after_first"),
        CheckConstraint(
            "location IS NULL OR ST_CoveredBy(location, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="location_wgs84_bounds",
        ),
        CheckConstraint(
            "affected_area IS NULL OR ST_CoveredBy(affected_area, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="affected_area_wgs84_bounds",
        ),
        Index("ix_crisis_events_type_status", "crisis_type", "status"),
        Index("ix_crisis_events_started_at", "started_at"),
        Index("ix_crisis_events_expires_at", "expires_at"),
        Index(
            "ix_crisis_events_location_gist",
            "location",
            postgresql_using="gist",
        ),
        Index(
            "ix_crisis_events_affected_area_gist",
            "affected_area",
            postgresql_using="gist",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    crisis_type: Mapped[CrisisType] = mapped_column(
        enum_type(CrisisType, "crisis_type"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[SeverityLevel | None] = mapped_column(
        enum_type(SeverityLevel, "severity_level"),
        nullable=True,
    )
    status: Mapped[CrisisStatus] = mapped_column(
        enum_type(CrisisStatus, "crisis_status"),
        nullable=False,
        default=CrisisStatus.UNKNOWN,
        server_default=CrisisStatus.UNKNOWN.value,
    )
    location: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    affected_area: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
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

    source_reports: Mapped[list[CrisisSourceReport]] = relationship(
        back_populates="crisis_event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    hazard_observations: Mapped[list[HazardObservation]] = relationship(
        back_populates="crisis_event",
        passive_deletes=True,
    )
    recommendation_runs: Mapped[list[RecommendationRun]] = relationship(
        back_populates="crisis_event",
        passive_deletes="all",
    )


class CrisisSourceReport(Base):
    __tablename__ = "crisis_source_reports"
    __table_args__ = (
        UniqueConstraint(
            "data_source_id",
            "external_event_id",
            name="uq_crisis_source_reports_source_external_id",
        ),
        CheckConstraint(
            "valid_until IS NULL OR issued_at IS NULL OR valid_until >= issued_at",
            name="valid_until_after_issued",
        ),
        Index("ix_crisis_source_reports_crisis_event_id", "crisis_event_id"),
        Index("ix_crisis_source_reports_data_source_id", "data_source_id"),
        Index("ix_crisis_source_reports_issued_at", "issued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    crisis_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crisis_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_event_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_severity: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    raw_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    crisis_event: Mapped[CrisisEvent] = relationship(back_populates="source_reports")
    data_source: Mapped[DataSource] = relationship(back_populates="crisis_reports")


class HazardObservation(Base):
    __tablename__ = "hazard_observations"
    __table_args__ = (
        CheckConstraint(
            "geometry IS NULL OR ST_CoveredBy(geometry, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="geometry_wgs84_bounds",
        ),
        CheckConstraint(
            "length(trim(observation_type)) > 0",
            name="observation_type_nonempty",
        ),
        Index("ix_hazard_observations_crisis_event_id", "crisis_event_id"),
        Index("ix_hazard_observations_data_source_id", "data_source_id"),
        Index(
            "ix_hazard_observations_source_observed",
            "data_source_id",
            "observed_at",
        ),
        Index(
            "ix_hazard_observations_geometry_gist",
            "geometry",
            postgresql_using="gist",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    crisis_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("crisis_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    observation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    value_numeric: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8),
        nullable=True,
    )
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    geometry: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=True,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    crisis_event: Mapped[CrisisEvent | None] = relationship(
        back_populates="hazard_observations",
    )
    data_source: Mapped[DataSource] = relationship(
        back_populates="hazard_observations",
    )
