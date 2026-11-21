from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field

from app.models.enums import CrisisType, DesignationStatus, VerificationStatus
from app.schemas.common import SchemaModel
from app.schemas.disaster_search import DisasterEventSummary, UserLocation
from app.schemas.recommendation import SuitabilityClassification
from app.schemas.route import RouteClassification, RouteDetailItem


class UnifiedRecommendationStatus(str, Enum):
    RECOMMENDATIONS_AVAILABLE = "RECOMMENDATIONS_AVAILABLE"
    LIMITED_RECOMMENDATIONS = "LIMITED_RECOMMENDATIONS"
    NO_SUITABLE_DESTINATION_FOUND = "NO_SUITABLE_DESTINATION_FOUND"
    NO_ROUTE_AVAILABLE = "NO_ROUTE_AVAILABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DISASTER_CONTEXT_UNAVAILABLE = "DISASTER_CONTEXT_UNAVAILABLE"
    ASSESSMENT_FAILED = "ASSESSMENT_FAILED"


class DisasterHazardAssessmentSummary(SchemaModel):
    disaster_type: CrisisType
    status: str
    summary: str
    data_freshness: str = "UNKNOWN"
    key_hazards: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RecommendedDestinationItem(SchemaModel):
    destination_id: UUID
    assessment_id: UUID | None = None
    name: str | None = None
    category: str
    official_designation: DesignationStatus
    verification_status: VerificationStatus
    verification_detail: str = "UNVERIFIED"
    coordinates: dict[str, float]
    distance_km: float
    suitability_classification: SuitabilityClassification
    suitability_score: float | None = None
    distance_points: float | None = None
    safety_points: float | None = None
    availability_points: float | None = None
    features_points: float | None = None
    total_points: float | None = None
    suitability_reasons: list[str] = Field(default_factory=list)
    suitability_warnings: list[str] = Field(default_factory=list)
    route: RouteDetailItem | None = None
    route_status: RouteClassification | None = None
    ranking_score: float | None = None
    ranking_rationale: str


class UnifiedRecommendationRequest(SchemaModel):
    latitude: float = Field(
        ge=-90.0, le=90.0, description="User origin latitude in WGS84 coordinates"
    )
    longitude: float = Field(
        ge=-180.0, le=180.0, description="User origin longitude in WGS84 coordinates"
    )
    disaster_type: CrisisType = Field(
        description="Disaster type context: EARTHQUAKE, FLOOD, or CYCLONE"
    )
    crisis_event_id: UUID | None = Field(
        default=None, description="Optional active CrisisEvent ID for the context disaster"
    )
    search_radius_km: float = Field(
        default=25.0,
        gt=0.0,
        le=500.0,
        description="Search radius in kilometers for destination discovery",
    )
    max_destinations: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of recommended destinations to return with calculated routes",
    )


class UnifiedRecommendationResponse(SchemaModel):
    success: bool = True
    status: UnifiedRecommendationStatus
    user_location: UserLocation
    disaster_type: CrisisType
    crisis_event: DisasterEventSummary | None = None
    hazard_assessment: DisasterHazardAssessmentSummary
    total_candidates_found: int = 0
    total_suitable_destinations: int = 0
    total_routed_destinations: int = 0
    recommendations: list[RecommendedDestinationItem] = Field(default_factory=list)
    excluded_destinations_count: int = 0
    methodology_and_ranking_notes: list[str] = Field(default_factory=list)
    overall_uncertainties: list[str] = Field(default_factory=list)
    safety_disclaimer: str


__all__ = [
    "DisasterHazardAssessmentSummary",
    "RecommendedDestinationItem",
    "UnifiedRecommendationRequest",
    "UnifiedRecommendationResponse",
    "UnifiedRecommendationStatus",
]
