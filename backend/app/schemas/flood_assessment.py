from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from app.models.enums import CrisisStatus, SeverityLevel
from app.schemas.common import SchemaModel
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation


class GeographicalEnvironment(str, Enum):
    PLAIN_OR_FLAT = "PLAIN_OR_FLAT"
    HILLY_OR_MOUNTAINOUS = "HILLY_OR_MOUNTAINOUS"
    UNDETERMINED = "UNDETERMINED"


class FloodAssessmentStatus(str, Enum):
    OBSERVED_FLOODING = "OBSERVED_FLOODING"
    OFFICIAL_FLOOD_WARNING = "OFFICIAL_FLOOD_WARNING"
    ELEVATED_FLOOD_SUSCEPTIBILITY = "ELEVATED_FLOOD_SUSCEPTIBILITY"
    HIGH_FLOW_PATH_EXPOSURE = "HIGH_FLOW_PATH_EXPOSURE"
    UPSTREAM_CATCHMENT_CONCERN = "UPSTREAM_CATCHMENT_CONCERN"
    VALLEY_FLOOD_CONCERN = "VALLEY_FLOOD_CONCERN"
    LANDSLIDE_RELATED_CONCERN = "LANDSLIDE_RELATED_CONCERN"
    DEBRIS_FLOW_CONCERN = "DEBRIS_FLOW_CONCERN"
    NO_RELEVANT_FLOOD_EVIDENCE = "NO_RELEVANT_FLOOD_EVIDENCE"
    NO_RELEVANT_HAZARD_EVIDENCE = "NO_RELEVANT_HAZARD_EVIDENCE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_STALE = "DATA_STALE"


class TerrainHazardStatus(str, Enum):
    OBSERVED_FLOODING = "OBSERVED_FLOODING"
    OFFICIAL_FLOOD_WARNING = "OFFICIAL_FLOOD_WARNING"
    HIGH_FLOW_PATH_EXPOSURE = "HIGH_FLOW_PATH_EXPOSURE"
    UPSTREAM_CATCHMENT_CONCERN = "UPSTREAM_CATCHMENT_CONCERN"
    VALLEY_FLOOD_CONCERN = "VALLEY_FLOOD_CONCERN"
    LANDSLIDE_RELATED_CONCERN = "LANDSLIDE_RELATED_CONCERN"
    DEBRIS_FLOW_CONCERN = "DEBRIS_FLOW_CONCERN"
    NO_RELEVANT_HAZARD_EVIDENCE = "NO_RELEVANT_HAZARD_EVIDENCE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_STALE = "DATA_STALE"


class RiverLevelStatus(str, Enum):
    BELOW_WARNING = "BELOW_WARNING"
    ABOVE_WARNING = "ABOVE_WARNING"
    ABOVE_DANGER = "ABOVE_DANGER"
    ABOVE_HFL = "ABOVE_HFL"
    UNKNOWN = "UNKNOWN"


class ObservedFloodEvidence(SchemaModel):
    event_id: uuid.UUID
    title: str | None = None
    description: str | None = None
    observed_at: datetime | None = None
    distance_km: float
    intersects_user_location: bool = False
    source_name: str
    reported_extent_or_depth: str | None = None


class FloodWarningEvidence(SchemaModel):
    event_id: uuid.UUID
    title: str | None = None
    description: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus
    warning_issued_at: datetime | None = None
    valid_until: datetime | None = None
    distance_km: float
    intersects_user_location: bool = False
    source_name: str
    headline: str | None = None
    warning_level: str | None = None  # e.g., "Red Warning", "Orange Alert", "Yellow Watch"


class RiverObservationEvidence(SchemaModel):
    station_code: str
    station_name: str | None = None
    river_name: str | None = None
    basin: str | None = None
    water_level_m: float | None = None  # Never represented as 0.0 when missing
    warning_level_m: float | None = None
    danger_level_m: float | None = None
    hfl_m: float | None = None  # Highest Flood Level
    distance_km: float
    observed_at: datetime
    status_relative_to_danger: RiverLevelStatus = RiverLevelStatus.UNKNOWN


class RainfallEvidence(SchemaModel):
    station_or_district: str | None = None
    rainfall_intensity_mm_hr: float | None = None  # Never represented as 0.0 when missing
    accumulation_period_hours: int | None = None
    rainfall_category: str | None = None  # "LIGHT", "MODERATE", "HEAVY", "VERY_HEAVY", "EXTREMELY_HEAVY"
    observed_or_forecast: str = "OBSERVED"  # "OBSERVED", "NOWCAST_FORECAST", "UNKNOWN"
    distance_km: float
    observed_at: datetime
    warning_color: str | None = None


class TerrainEvidence(SchemaModel):
    available: bool = False
    elevation_m: float | None = None
    slope_degrees: float | None = None
    aspect: str | None = None
    flow_accumulation: float | None = None
    drainage_proximity_m: float | None = None
    upstream_catchment_area_km2: float | None = None
    flow_direction_deg: float | None = None
    topographic_wetness_index: float | None = None
    relative_elevation_above_drainage_m: float | None = None
    valley_confinement: str | None = None  # "NARROW_CANYON", "V_SHAPED_VALLEY", "CONVERGENT_HOLLOW", "OPEN_VALLEY", "RIDGE_OR_PEAK"
    geographical_environment: GeographicalEnvironment = GeographicalEnvironment.UNDETERMINED
    notes: list[str] = Field(default_factory=list)


class HistoricalFloodEvidence(SchemaModel):
    available: bool = False
    historical_events_count: int = 0
    last_recorded_event_date: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class HillyTerrainAnalysis(SchemaModel):
    applied: bool = False
    dem_source: str | None = None
    dem_resolution_m: float | None = None
    upstream_flow_path_exposure: bool = False
    upstream_catchment_concern: bool = False
    valley_flood_concern: bool = False
    landslide_susceptibility_concern: bool = False
    debris_flow_susceptibility_concern: bool = False
    flash_flood_concern: bool = False
    upstream_rainfall_concern: bool = False
    terrain_hazard_statuses: list[TerrainHazardStatus] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    limitations_and_assumptions: list[str] = Field(default_factory=list)


class FloodAssessmentSearchParameters(SchemaModel):
    lookback_days: int
    search_radius_km: float


class FloodAssessmentResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    assessment_timestamp: datetime
    geographical_environment: GeographicalEnvironment
    assessment_status: FloodAssessmentStatus
    terrain_hazard_statuses: list[TerrainHazardStatus] = Field(default_factory=list)
    summary_message: str
    search_parameters: FloodAssessmentSearchParameters
    observed_flooding: list[ObservedFloodEvidence] = Field(default_factory=list)
    official_warnings: list[FloodWarningEvidence] = Field(default_factory=list)
    river_observations: list[RiverObservationEvidence] = Field(default_factory=list)
    rainfall_information: list[RainfallEvidence] = Field(default_factory=list)
    terrain_information: TerrainEvidence
    historical_flood_information: HistoricalFloodEvidence
    hilly_terrain_analysis: HillyTerrainAnalysis | None = None
    contributing_factors: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    data_freshness: str
    source_attribution: list[DisasterSourceSummary] = Field(default_factory=list)
    scientific_limitations: list[str] = Field(default_factory=list)
