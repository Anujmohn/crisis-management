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
    """Calculated fields for Flood safe location discovery and assessment."""
    distance_from_user_km: float
    elevation_m: float | None = None  # None if UNKNOWN
    safety_factor: str
    is_safe_building: bool
    safe_building_type: str
    flood_flow_prediction: str
    middle_transect_heights_msl: list[float] = Field(default_factory=list)
    is_elevated_above_transect: bool = True
    transect_summary: str = ""
    elevation_difference_from_user_m: float | None = None
    is_higher_than_user: bool = True
    nearest_city: str | None = None

    # Flood-specific discovery and assessment fields (Requirements 2, 3, 4, 5, 6, 8)
    candidate_type: str = "ELEVATED_INFRASTRUCTURE"  # ELEVATED_INFRASTRUCTURE, NATURAL_ELEVATED_TERRAIN, OFFICIAL_FLOOD_SHELTER, GENERAL_ELEVATED_FACILITY, UNSUITABLE_FLOOD_RISK
    elevation_status: str = "VERIFIED_DEM"  # VERIFIED_DEM, CITY_MSL_BENCHMARK, UNKNOWN
    elevation_source: str | None = "Copernicus DEM GLO-30 / Survey of India Benchmark"
    elevation_resolution_m: float | None = 30.0
    elevation_retrieval_time: str | None = None
    elevation_quality: str = "HIGH"  # HIGH, INTERPOLATED, UNKNOWN
    relative_elevation_m: float | None = None
    local_terrain_slope_deg: float | None = None
    terrain_feature_type: str = "RIDGE_OR_HIGH_GROUND"  # RIDGE_OR_HIGH_GROUND, TERRACE, FLAT_PLAIN, VALLEY_BOTTOM, DRAINAGE_CHANNEL
    drainage_proximity_m: float | None = None
    flow_accumulation_cells: float | None = None
    flood_exposure_level: str = "MINIMAL"  # MINIMAL, MODERATE, HIGH_RUNOFF, SEVERE_INUNDATION_RISK
    safety_score: float = 75.0
    safety_category: str = "SUITABLE_ELEVATED_REFUGE"  # SUITABLE_ELEVATED_REFUGE, CONDITIONALLY_ELEVATED, INSUFFICIENT_ELEVATION, HAZARDOUS_FLOOD_PATH, EXCLUDED
    assessment_reasons: list[str] = Field(default_factory=list)
    known_hazards: list[str] = Field(default_factory=list)
    data_confidence: str = "HIGH"  # HIGH, MODERATE, LOW, INSUFFICIENT_DATA
    search_radius_m: float = 100.0
    public_access_status: str = "UNKNOWN_VERIFICATION_REQUIRED"
    last_updated_time: str | None = None


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

    # Flood & Candidate Metadata (Requirements 2, 3, 5, 8)
    candidate_type: str = "ELEVATED_INFRASTRUCTURE"  # ELEVATED_INFRASTRUCTURE, NATURAL_ELEVATED_TERRAIN, OFFICIAL_FLOOD_SHELTER, GENERAL_ELEVATED_FACILITY, UNSUITABLE_FLOOD_RISK
    discovery_source: str = "OpenStreetMap / Real Infrastructure"
    search_radius_m: float = 100.0
    elevation_m: float | None = None
    relative_elevation_m: float | None = None
    data_confidence: str = "HIGH"
    assessment_reasons: list[str] = Field(default_factory=list)
    known_hazards: list[str] = Field(default_factory=list)
    last_updated_time: str | None = None

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
    engine_status: str = "SUCCESS"  # SUCCESS, NO_CANDIDATES_FOUND, NO_RELIABLE_ELEVATION, FLOOD_ASSESSMENT_INCOMPLETE, ONLY_UNVERIFIED_CANDIDATES, NO_SUITABLE_CANDIDATE
    engine_message: str = "Candidate safe places evaluated successfully."
    search_radii_hierarchy_m: list[float] = Field(default_factory=lambda: [100.0, 250.0, 500.0, 1000.0, 2000.0])
    disaster_safety_disclaimer: str = (
        "Automated safe place recommendations are advisory models for emergency planning. "
        "Always follow official evacuation directives from NDMA, IMD, CWC, and local state authorities."
    )


class TransectNodeItem(SchemaModel):
    index: int
    fraction: float
    latitude: float
    longitude: float
    height_from_sea_level_m: float
    distance_from_crisis_km: float
    distance_to_user_km: float
    nearest_city: str
    stage_description: str


class ElevationTransectSummary(SchemaModel):
    crisis_start_latitude: float
    crisis_start_longitude: float
    crisis_start_height_msl: float
    crisis_start_nearest_city: str
    user_latitude: float
    user_longitude: float
    user_height_msl: float
    user_nearest_city: str
    total_distance_km: float
    height_difference_m: float
    higher_location_name: str
    lower_location_name: str
    is_crisis_higher: bool
    is_user_higher: bool
    flow_direction: str
    gradient_slope_percent: float
    middle_transect_nodes: list[TransectNodeItem] = Field(default_factory=list)
    middle_heights_msl: list[float] = Field(default_factory=list)
    min_middle_height_msl: float
    max_middle_height_msl: float
    avg_middle_height_msl: float
    has_intermediate_ridge: bool
    has_intermediate_depression: bool
    flow_prediction_summary: str
    elevation_difference_summary: str


class ElevatedPlaceSummaryItem(SchemaModel):
    id: uuid.UUID
    name: str
    category: str
    place_type: str
    latitude: float
    longitude: float
    distance_m: float
    distance_km: float
    elevation_msl_m: float
    elevation_gain_above_user_m: float
    nearest_city: str
    elevation_comparison: dict[str, Any] = Field(default_factory=dict)
    location_geojson: dict[str, Any] = Field(default_factory=dict)
    verification_status: str


class ElevatedPlacesDiscoveryResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    user_height_msl: float
    user_nearest_city: str
    initial_radius_km: float = 0.1
    effective_radius_km: float
    total_found: int
    elevated_places: list[ElevatedPlaceSummaryItem] = Field(default_factory=list)
    engine_name: str = "Elevated Place Discovery Engine (More Height Than User Location)"


class FloodElevationAnalysisResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    user_height_msl: float
    user_nearest_city: str
    crisis_start_point: dict[str, Any]
    elevation_transect: ElevationTransectSummary
    elevated_safe_places: list[ElevatedPlaceSummaryItem] = Field(default_factory=list)
    flood_flow_path_geojson: dict[str, Any] = Field(default_factory=dict)
    combined_safety_summary: str
