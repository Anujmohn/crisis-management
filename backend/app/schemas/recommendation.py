from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.models.enums import (
    CrisisType,
    DesignationStatus,
    RecommendationRunStatus,
    RiskLevel,
    VerificationStatus,
)
from app.schemas.common import (
    DistanceMeters,
    GeoJSONPoint,
    JsonObject,
    JsonValue,
    NonEmptyString,
    ORMReadSchema,
    SchemaModel,
    UnitIntervalScore,
)
from app.schemas.disaster_search import UserLocation


class SuitabilityClassification(str, Enum):
    SUITABILITY_SUPPORTED_BY_EVIDENCE = "SUITABILITY_SUPPORTED_BY_EVIDENCE"
    CONDITIONALLY_SUITABLE = "CONDITIONALLY_SUITABLE"
    NOT_RECOMMENDED = "NOT_RECOMMENDED"
    EXCLUDED = "EXCLUDED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNKNOWN = "UNKNOWN"


class RecommendationRunCreate(SchemaModel):
    crisis_event_id: UUID | None = None
    user_location: GeoJSONPoint
    engine_version: NonEmptyString = Field(max_length=100)
    status: RecommendationRunStatus = RecommendationRunStatus.PENDING


class RecommendationRunUpdate(SchemaModel):
    crisis_event_id: UUID | None = Field(default=None)
    user_location: GeoJSONPoint | None = Field(default=None)
    engine_version: NonEmptyString | None = Field(default=None, max_length=100)
    status: RecommendationRunStatus | None = Field(default=None)


class RecommendationRunRead(ORMReadSchema):
    id: UUID
    crisis_event_id: UUID | None
    user_location: GeoJSONPoint
    requested_at: AwareDatetime
    engine_version: NonEmptyString
    status: RecommendationRunStatus


class LocationAssessmentCreate(SchemaModel):
    recommendation_run_id: UUID
    safe_location_id: UUID
    suitability_score: UnitIntervalScore | None = None
    risk_level: RiskLevel = RiskLevel.UNKNOWN
    distance_m: DistanceMeters | None = None
    factors: JsonObject = Field(default_factory=dict)
    reasons: list[JsonValue] = Field(default_factory=list)
    warnings: list[JsonValue] = Field(default_factory=list)
    data_completeness: UnitIntervalScore | None = None


class LocationAssessmentRead(ORMReadSchema):
    id: UUID
    recommendation_run_id: UUID
    safe_location_id: UUID
    suitability_score: UnitIntervalScore | None
    risk_level: RiskLevel
    distance_m: DistanceMeters | None
    factors: JsonObject
    reasons: list[JsonValue]
    warnings: list[JsonValue]
    data_completeness: UnitIntervalScore | None
    assessed_at: AwareDatetime


class LocationSuitabilityAssessmentItem(SchemaModel):
    assessment_id: UUID | None = None
    location_id: UUID
    name: str | None = None
    category: str
    official_designation: DesignationStatus
    verification_status: VerificationStatus
    verification_detail: str = "UNVERIFIED"
    distance_km: float
    disaster_type: CrisisType
    suitability_classification: SuitabilityClassification
    suitability_score: float | None = None
    data_completeness: float | None = None
    distance_points: float | None = None
    safety_points: float | None = None
    availability_points: float | None = None
    features_points: float | None = None
    total_points: float | None = None
    hard_exclusion_status: bool = False
    hard_exclusion_reasons: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    identified_hazards: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    data_freshness: str = "UNKNOWN"
    assessed_at: AwareDatetime


class AssessRecommendationsRequest(SchemaModel):
    latitude: float = Field(ge=-90.0, le=90.0, description="User latitude in WGS84 coordinates")
    longitude: float = Field(ge=-180.0, le=180.0, description="User longitude in WGS84 coordinates")
    disaster_type: CrisisType = Field(description="Disaster type: EARTHQUAKE, FLOOD, or CYCLONE")
    crisis_event_id: UUID | None = Field(default=None, description="Optional associated CrisisEvent ID")
    search_radius_km: float = Field(default=25.0, gt=0.0, le=500.0, description="Search radius in kilometers")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of candidate locations to assess")


class AssessRecommendationsResponse(SchemaModel):
    success: bool = True
    recommendation_run_id: UUID
    user_location: UserLocation
    disaster_type: CrisisType
    crisis_event_id: UUID | None = None
    total_candidates_assessed: int = 0
    total_potentially_suitable: int = 0
    total_conditionally_suitable: int = 0
    total_not_recommended: int = 0
    total_excluded: int = 0
    total_insufficient_data: int = 0
    assessed_locations: list[LocationSuitabilityAssessmentItem] = Field(default_factory=list)
    assessment_methodology: str
    overall_data_limitations: list[str] = Field(default_factory=list)
    safety_disclaimer: str


__all__ = [
    "AssessRecommendationsRequest",
    "AssessRecommendationsResponse",
    "LocationAssessmentCreate",
    "LocationAssessmentRead",
    "LocationSuitabilityAssessmentItem",
    "RecommendationRunCreate",
    "RecommendationRunRead",
    "RecommendationRunUpdate",
    "SuitabilityClassification",
]
