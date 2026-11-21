from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
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
from app.models.enums import (
    AccessibilityStatus,
    DesignationStatus,
    RecommendationRunStatus,
    RiskLevel,
    RouteSafetyStatus,
    StructuralCondition,
    VerificationStatus,
    enum_type,
)


class SafeLocation(Base):
    __tablename__ = "safe_locations"
    __table_args__ = (
        CheckConstraint("length(trim(category)) > 0", name="category_nonempty"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="capacity_nonnegative"),
        CheckConstraint(
            "ST_CoveredBy(location, ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="location_wgs84_bounds",
        ),
        CheckConstraint(
            "boundary IS NULL OR GeometryType(boundary) "
            "IN ('POLYGON', 'MULTIPOLYGON')",
            name="boundary_polygonal",
        ),
        CheckConstraint(
            "boundary IS NULL OR ST_CoveredBy(boundary, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="boundary_wgs84_bounds",
        ),
        Index("ix_safe_locations_category_designation", "category", "designation_status"),
        Index("ix_safe_locations_verification_status", "verification_status"),
        Index(
            "ix_safe_locations_location_gist",
            "location",
            postgresql_using="gist",
        ),
        Index(
            "ix_safe_locations_boundary_gist",
            "boundary",
            postgresql_using="gist",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    designation_status: Mapped[DesignationStatus] = mapped_column(
        enum_type(DesignationStatus, "designation_status"),
        nullable=False,
        default=DesignationStatus.UNKNOWN,
        server_default=DesignationStatus.UNKNOWN.value,
    )
    location: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    boundary: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=True,
    )
    structural_condition: Mapped[StructuralCondition | None] = mapped_column(
        enum_type(StructuralCondition, "structural_condition"),
        nullable=True,
    )
    accessibility_status: Mapped[AccessibilityStatus | None] = mapped_column(
        enum_type(AccessibilityStatus, "accessibility_status"),
        nullable=True,
    )
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_type(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
        server_default=VerificationStatus.UNVERIFIED.value,
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

    source_references: Mapped[list[SafeLocationSourceReference]] = relationship(
        back_populates="safe_location",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    assessments: Mapped[list[LocationAssessment]] = relationship(
        back_populates="safe_location",
        passive_deletes="all",
    )


class SafeLocationSourceReference(Base):
    __tablename__ = "safe_location_source_references"
    __table_args__ = (
        UniqueConstraint(
            "data_source_id",
            "external_feature_id",
            name="uq_safe_location_refs_source_external_id",
        ),
        Index("ix_safe_location_refs_safe_location_id", "safe_location_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    safe_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("safe_locations.id", ondelete="CASCADE"),
        nullable=False,
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_feature_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    properties: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    safe_location: Mapped[SafeLocation] = relationship(
        back_populates="source_references",
    )
    data_source: Mapped[DataSource] = relationship(
        back_populates="safe_location_references",
    )


class RecommendationRun(Base):
    __tablename__ = "recommendation_runs"
    __table_args__ = (
        CheckConstraint(
            "ST_CoveredBy(user_location, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="user_location_wgs84_bounds",
        ),
        CheckConstraint("length(trim(engine_version)) > 0", name="engine_version_nonempty"),
        Index("ix_recommendation_runs_crisis_event_id", "crisis_event_id"),
        Index("ix_recommendation_runs_requested_at", "requested_at"),
        Index(
            "ix_recommendation_runs_user_location_gist",
            "user_location",
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
    user_location: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[RecommendationRunStatus] = mapped_column(
        enum_type(RecommendationRunStatus, "recommendation_run_status"),
        nullable=False,
        default=RecommendationRunStatus.PENDING,
        server_default=RecommendationRunStatus.PENDING.value,
    )

    crisis_event: Mapped[CrisisEvent | None] = relationship(
        back_populates="recommendation_runs",
    )
    assessments: Mapped[list[LocationAssessment]] = relationship(
        back_populates="recommendation_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class LocationAssessment(Base):
    __tablename__ = "location_assessments"
    __table_args__ = (
        UniqueConstraint(
            "recommendation_run_id",
            "safe_location_id",
            name="uq_location_assessments_run_location",
        ),
        CheckConstraint(
            "suitability_score IS NULL OR "
            "(suitability_score >= 0 AND suitability_score <= 1)",
            name="suitability_score_range",
        ),
        CheckConstraint("distance_m IS NULL OR distance_m >= 0", name="distance_nonnegative"),
        CheckConstraint(
            "data_completeness IS NULL OR "
            "(data_completeness >= 0 AND data_completeness <= 1)",
            name="data_completeness_range",
        ),
        Index("ix_location_assessments_safe_location_id", "safe_location_id"),
        Index("ix_location_assessments_risk_level", "risk_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    recommendation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    safe_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("safe_locations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    suitability_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_type(RiskLevel, "risk_level"),
        nullable=False,
        default=RiskLevel.UNKNOWN,
        server_default=RiskLevel.UNKNOWN.value,
    )
    distance_m: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    factors: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    data_completeness: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    recommendation_run: Mapped[RecommendationRun] = relationship(
        back_populates="assessments",
    )
    safe_location: Mapped[SafeLocation] = relationship(back_populates="assessments")
    routes: Mapped[list[EvacuationRoute]] = relationship(
        back_populates="location_assessment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EvacuationRoute(Base):
    __tablename__ = "evacuation_routes"
    __table_args__ = (
        CheckConstraint(
            "ST_CoveredBy(route_geometry, "
            "ST_MakeEnvelope(-180, -90, 180, 90, 4326))",
            name="route_geometry_wgs84_bounds",
        ),
        CheckConstraint("distance_m IS NULL OR distance_m >= 0", name="distance_nonnegative"),
        CheckConstraint(
            "estimated_time_seconds IS NULL OR estimated_time_seconds >= 0",
            name="estimated_time_nonnegative",
        ),
        Index(
            "ix_evacuation_routes_location_assessment_id",
            "location_assessment_id",
        ),
        Index(
            "ix_evacuation_routes_geometry_gist",
            "route_geometry",
            postgresql_using="gist",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    location_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("location_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    route_geometry: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False),
        nullable=False,
    )
    distance_m: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    estimated_time_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    hazard_intersections: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    route_safety_status: Mapped[RouteSafetyStatus] = mapped_column(
        enum_type(RouteSafetyStatus, "route_safety_status"),
        nullable=False,
        default=RouteSafetyStatus.UNKNOWN,
        server_default=RouteSafetyStatus.UNKNOWN.value,
    )
    routing_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    location_assessment: Mapped[LocationAssessment] = relationship(
        back_populates="routes",
    )
