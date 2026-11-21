from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.models.enums import CrisisType, RouteSafetyStatus
from app.schemas.common import (
    DistanceMeters,
    GeoJSONLineString,
    JsonValue,
    NonNegativeInteger,
    ORMReadSchema,
    SchemaModel,
)


class RouteClassification(str, Enum):
    ROUTE_AVAILABLE = "ROUTE_AVAILABLE"
    ROUTE_WITH_WARNINGS = "ROUTE_WITH_WARNINGS"
    ROUTE_EXCLUDED = "ROUTE_EXCLUDED"
    NO_SAFE_ROUTE_FOUND = "NO_SAFE_ROUTE_FOUND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ROUTING_SERVICE_UNAVAILABLE = "ROUTING_SERVICE_UNAVAILABLE"


class EvacuationRouteCreate(SchemaModel):
    location_assessment_id: UUID
    route_geometry: GeoJSONLineString
    distance_m: DistanceMeters | None = None
    estimated_time_seconds: NonNegativeInteger | None = None
    hazard_intersections: list[JsonValue] = Field(default_factory=list)
    route_safety_status: RouteSafetyStatus = RouteSafetyStatus.UNKNOWN
    routing_provider: str | None = Field(default=None, max_length=100)


class EvacuationRouteRead(ORMReadSchema):
    id: UUID
    location_assessment_id: UUID
    route_geometry: GeoJSONLineString
    distance_m: DistanceMeters | None
    estimated_time_seconds: NonNegativeInteger | None
    hazard_intersections: list[JsonValue]
    route_safety_status: RouteSafetyStatus
    routing_provider: str | None
    calculated_at: AwareDatetime


class HazardIntersectionItem(SchemaModel):
    hazard_id: str | None = None
    hazard_type: str
    severity: str | None = None
    distance_to_route_m: float
    is_direct_intersection: bool
    is_hard_exclusion: bool = False
    description: str
    source_name: str | None = None
    reported_at: str | None = None
    approximate_segment_coords: list[float] | None = None


class RouteDetailItem(SchemaModel):
    route_id: UUID
    route_geometry: GeoJSONLineString
    distance_m: float
    distance_km: float
    estimated_time_seconds: int
    estimated_time_minutes: float
    route_status: RouteClassification
    hazard_exposure_level: str  # "LOW", "MODERATE", "HIGH", "CRITICAL"
    hazard_intersections_count: int
    hazard_intersections: list[HazardIntersectionItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    is_hard_excluded: bool = False
    hard_exclusion_reasons: list[str] = Field(default_factory=list)
    comparison_notes: str | None = None
    routing_provider: str
    calculated_at: AwareDatetime


class CalculateRouteRequest(SchemaModel):
    origin_latitude: float = Field(
        ge=-90.0, le=90.0, description="User origin latitude in WGS84 coordinates"
    )
    origin_longitude: float = Field(
        ge=-180.0, le=180.0, description="User origin longitude in WGS84 coordinates"
    )
    location_assessment_id: UUID = Field(
        description="UUID of the assessed candidate destination"
    )
    allow_alternatives: bool = Field(
        default=True, description="Whether to request and analyze alternative routes"
    )


class CalculateRouteResponse(SchemaModel):
    success: bool = True
    route_status: RouteClassification
    origin: dict[str, float]
    destination_id: UUID
    destination_name: str | None = None
    destination_category: str | None = None
    disaster_type: CrisisType
    primary_route: RouteDetailItem | None = None
    alternative_routes: list[RouteDetailItem] = Field(default_factory=list)
    total_routes_analyzed: int = 0
    comparison_summary: str | None = None
    routing_provider: str
    routing_limitations: list[str] = Field(default_factory=list)
    safety_disclaimer: str


__all__ = [
    "CalculateRouteRequest",
    "CalculateRouteResponse",
    "EvacuationRouteCreate",
    "EvacuationRouteRead",
    "HazardIntersectionItem",
    "RouteClassification",
    "RouteDetailItem",
]
