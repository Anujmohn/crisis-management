from __future__ import annotations

import uuid
from typing import Any
from pydantic import Field
from pydantic.types import AwareDatetime

from app.models.enums import (
    AccessibilityStatus,
    CrisisType,
    DesignationStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.schemas.common import (
    GeoJSONMultiPolygon,
    GeoJSONPoint,
    GeoJSONPolygon,
    SchemaModel,
)
from app.schemas.disaster_search import UserLocation
from app.schemas.recommendation import SuitabilityClassification
from app.schemas.safe_location import SafeLocationSourceSummary


class SpecificationMatchPoints(SchemaModel):
    """Specification match points scoring (0 to 100 points)."""
    distance_points: float = 0.0
    safety_factor_points: float = 0.0
    availability_points: float = 0.0
    features_points: float = 0.0
    total_points: float = 0.0


class EarthquakeSafetyFields(SchemaModel):
    """Calculated fields for Earthquake safe location discovery."""
    distance_from_user_km: float
    distance_from_user_m: float
    safe_area_m2: float
    is_open_place: bool
    open_place_type: str | None = None
    building_collapse_protection: str
    hilly_area_safety: str
    stone_crushing_risk: str


class FloodSafetyFields(SchemaModel):
    """Calculated fields for Flood safe location discovery."""
    distance_from_user_km: float
    elevation_m: float
    safety_factor: str
    is_safe_building: bool
    safe_building_type: str
    flood_flow_prediction: str
    middle_transect_heights_msl: list[float] = Field(default_factory=list)
    is_elevated_above_transect: bool
    transect_summary: str


class CycloneSafetyFields(SchemaModel):
    """Calculated fields for Sea Typhoon / Cyclone safe location discovery."""
    distance_from_user_km: float
    affect_of_typhoon_on_area: str
    distance_to_coastline_km: float
    is_beach_area: bool
    safety_factor_of_area: str
    cyclone_shelter_engineering: str


class SafePlaceItem(SchemaModel):
    """
    Candidate safe place evaluated by the Separate Safe Place Engine.
    Compatible with SafeLocationItem while carrying complete disaster field evaluations.
    """
    id: uuid.UUID
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

    # Distance
    distance_km: float
    distance_m: float

    # Suitability
    suitability_classification: SuitabilityClassification
    suitability_score: float

    # Specification Match Points (0 - 100)
    specification_points: SpecificationMatchPoints | None = None
    total_points: float | None = None
    distance_points: float | None = None
    safety_points: float | None = None
    availability_points: float | None = None
    features_points: float | None = None

    # 1.a, 3.b, 3.c Detailed Calculations
    earthquake_fields: EarthquakeSafetyFields | None = None
    flood_fields: FloodSafetyFields | None = None
    cyclone_fields: CycloneSafetyFields | None = None

    # Evidence, Hazards & Warnings
    suitability_evidence: list[str] = Field(default_factory=list)
    key_hazards: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    sources: list[SafeLocationSourceSummary] = Field(default_factory=list)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class FindSafePlacesRequest(SchemaModel):
    latitude: float = Field(ge=-90.0, le=90.0, description="User latitude")
    longitude: float = Field(ge=-180.0, le=180.0, description="User longitude")
    disaster_type: CrisisType | None = Field(
        default=None,
        description="Crisis type to evaluate: EARTHQUAKE, FLOOD, or CYCLONE. Defaults to general multi-hazard safety.",
    )
    max_radius_km: float = Field(
        default=5.0,
        ge=0.5,
        le=5.0,
        description="Maximum search radius in km (under in the 1 to 5 km constraint).",
    )
    limit: int = Field(default=20, ge=1, le=50)


class FindSafePlacesResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    disaster_type: CrisisType | None = None
    initial_radius_km: float = 0.1  # Always starts at 100 meters
    effective_radius_km: float
    total_found: int
    safe_places: list[SafePlaceItem] = Field(default_factory=list)
    data_sources_queried: list[str] = Field(default_factory=list)
    engine_name: str = "Separate Safe Place Discovery Engine (0.1 to 5.0 km Progressive Expansion)"
    disaster_safety_disclaimer: str = (
        "Automated safe place recommendations are advisory models for emergency planning. "
        "Always follow official evacuation directives from NDMA, IMD, CWC, and local state authorities."
    )
