from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.models.enums import (
    AccessibilityStatus,
    DesignationStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.schemas.common import (
    GeoJSONMultiPolygon,
    GeoJSONPoint,
    GeoJSONPolygon,
    JsonObject,
    NonEmptyString,
    NonNegativeInteger,
    ORMReadSchema,
    SchemaModel,
)
from app.schemas.disaster_search import UserLocation
from app.schemas.ingestion import SourceSyncSummary


class SafeLocationCreate(SchemaModel):
    name: str | None = Field(default=None, max_length=255)
    category: NonEmptyString = Field(max_length=80)
    designation_status: DesignationStatus = DesignationStatus.UNKNOWN
    location: GeoJSONPoint
    boundary: GeoJSONPolygon | GeoJSONMultiPolygon | None = None
    structural_condition: StructuralCondition | None = None
    accessibility_status: AccessibilityStatus | None = None
    capacity: NonNegativeInteger | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED


class SafeLocationUpdate(SchemaModel):
    name: str | None = Field(default=None, max_length=255)
    category: NonEmptyString = Field(default=None, max_length=80)
    designation_status: DesignationStatus = Field(default=None)
    location: GeoJSONPoint = Field(default=None)
    boundary: GeoJSONPolygon | GeoJSONMultiPolygon | None = None
    structural_condition: StructuralCondition | None = None
    accessibility_status: AccessibilityStatus | None = None
    capacity: NonNegativeInteger | None = None
    verification_status: VerificationStatus = Field(default=None)


class SafeLocationRead(ORMReadSchema):
    id: UUID
    name: str | None
    category: NonEmptyString
    designation_status: DesignationStatus
    location: GeoJSONPoint
    boundary: GeoJSONPolygon | GeoJSONMultiPolygon | None
    structural_condition: StructuralCondition | None
    accessibility_status: AccessibilityStatus | None
    capacity: NonNegativeInteger | None
    verification_status: VerificationStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SafeLocationSourceReferenceCreate(SchemaModel):
    safe_location_id: UUID
    data_source_id: UUID
    external_feature_id: str | None = Field(default=None, max_length=512)
    source_url: str | None = Field(default=None, max_length=2048)
    properties: JsonObject | None = None


class SafeLocationSourceReferenceUpdate(SchemaModel):
    safe_location_id: UUID = Field(default=None)
    data_source_id: UUID = Field(default=None)
    external_feature_id: str | None = Field(default=None, max_length=512)
    source_url: str | None = Field(default=None, max_length=2048)
    last_seen_at: AwareDatetime = Field(default=None)
    properties: JsonObject | None = None


class SafeLocationSourceReferenceRead(ORMReadSchema):
    id: UUID
    safe_location_id: UUID
    data_source_id: UUID
    external_feature_id: str | None
    source_url: str | None
    last_seen_at: AwareDatetime
    properties: JsonObject | None


class SafeLocationSourceSummary(SchemaModel):
    data_source_id: UUID
    data_source_code: str
    data_source_name: str
    external_feature_id: str | None = None
    source_url: str | None = None
    last_seen_at: AwareDatetime
    verification_detail: str | None = None
    properties: dict[str, Any] | None = None


class SafeLocationItem(SchemaModel):
    id: UUID
    name: str | None = None
    category: str
    designation_status: DesignationStatus
    verification_status: VerificationStatus
    verification_detail: str = "UNVERIFIED"
    location: GeoJSONPoint
    boundary: GeoJSONPolygon | GeoJSONMultiPolygon | None = None
    structural_condition: StructuralCondition | None = None
    accessibility_status: AccessibilityStatus | None = None
    capacity: int | None = None
    distance_km: float | None = None
    sources: list[SafeLocationSourceSummary] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class NearbySafeLocationsResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    search_radius_km: float
    total_found: int
    locations: list[SafeLocationItem] = Field(default_factory=list)
    official_shelters_count: int = 0
    candidate_locations_count: int = 0
    open_assembly_areas_count: int = 0
    data_sources_queried: list[str] = Field(default_factory=list)
    data_integrity_notice: str


class SafeLocationDetailResponse(SchemaModel):
    success: bool = True
    location: SafeLocationItem
    verification_evidence: dict[str, Any] = Field(default_factory=dict)
    disaster_suitability_disclaimer: str


class SafeLocationSyncResponse(SchemaModel):
    success: bool = True
    synced_at: AwareDatetime
    sources: list[SourceSyncSummary] = Field(default_factory=list)
    total_created: int = 0
    total_updated: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    message: str


__all__ = [
    "NearbySafeLocationsResponse",
    "SafeLocationCreate",
    "SafeLocationDetailResponse",
    "SafeLocationItem",
    "SafeLocationRead",
    "SafeLocationSourceReferenceCreate",
    "SafeLocationSourceReferenceRead",
    "SafeLocationSourceReferenceUpdate",
    "SafeLocationSourceSummary",
    "SafeLocationSyncResponse",
    "SafeLocationUpdate",
]
