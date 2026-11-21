from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from app.models.enums import CrisisStatus, SeverityLevel
from app.schemas.common import SchemaModel
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation
from app.schemas.flood_assessment import GeographicalEnvironment


class CycloneAssessmentStatus(str, Enum):
    OFFICIAL_CYCLONE_WARNING = "OFFICIAL_CYCLONE_WARNING"
    CYCLONE_POSITION_RELEVANT = "CYCLONE_POSITION_RELEVANT"
    WIND_HAZARD_CONCERN = "WIND_HAZARD_CONCERN"
    STORM_SURGE_WARNING = "STORM_SURGE_WARNING"
    COASTAL_INUNDATION_CONCERN = "COASTAL_INUNDATION_CONCERN"
    HEAVY_RAINFALL_WARNING = "HEAVY_RAINFALL_WARNING"
    INLAND_FLOOD_CONCERN = "INLAND_FLOOD_CONCERN"
    LANDSLIDE_RELATED_CONCERN = "LANDSLIDE_RELATED_CONCERN"
    NO_RELEVANT_HAZARD_EVIDENCE = "NO_RELEVANT_HAZARD_EVIDENCE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_STALE = "DATA_STALE"


class CyclonePosition(SchemaModel):
    latitude: float
    longitude: float
    position_type: str = "OBSERVED"  # "OBSERVED" or "ESTIMATED"
    observed_at: datetime | None = None
    distance_km: float
    source_name: str


class CycloneTrackWaypoint(SchemaModel):
    waypoint_index: int
    latitude: float
    longitude: float
    position_type: str = "FORECAST"  # "CURRENT" or "FORECAST"
    forecast_timestamp: datetime | None = None
    distance_km: float
    estimated_max_wind_kmph: float | None = None


class CycloneWindHazard(SchemaModel):
    active_wind_hazard: bool = False
    max_sustained_wind_kmph: float | None = None
    gust_speed_kmph: float | None = None
    original_wind_value: float | None = None
    original_unit: str | None = None
    cyclone_intensity_category: str = "UNKNOWN"
    is_forecast: bool = False
    warning_description: str | None = None


class CycloneStormSurgeHazard(SchemaModel):
    surge_warning_active: bool = False
    estimated_surge_height_m: float | None = None
    peak_wave_height_m: float | None = None
    tide_level_m: float | None = None
    is_forecast: bool = False
    source_agency: str | None = None  # e.g. "INCOIS / IMD"
    notes: list[str] = Field(default_factory=list)


class CycloneCoastalHazard(SchemaModel):
    is_coastal_zone: bool = False
    distance_to_coastline_km: float | None = None
    coastal_inundation_concern: bool = False
    notes: list[str] = Field(default_factory=list)


class CycloneRainfallFloodHazard(SchemaModel):
    heavy_rainfall_warning: bool = False
    inland_flood_concern: bool = False
    max_rainfall_intensity_mm_hr: float | None = None
    rainfall_category: str | None = None
    river_gauges_above_danger: int = 0
    river_gauges_above_warning: int = 0
    notes: list[str] = Field(default_factory=list)


class CycloneTerrainHazard(SchemaModel):
    geographical_environment: GeographicalEnvironment = GeographicalEnvironment.UNDETERMINED
    landslide_concern: bool = False
    debris_flow_concern: bool = False
    notes: list[str] = Field(default_factory=list)


class CycloneWarningEvidence(SchemaModel):
    event_id: uuid.UUID
    title: str | None = None
    description: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus
    issued_at: datetime | None = None
    valid_until: datetime | None = None
    distance_km: float
    intersects_user_location: bool = False
    source_name: str
    headline: str | None = None
    warning_type: str | None = None


class CycloneEventSummary(SchemaModel):
    event_id: uuid.UUID
    title: str | None = None
    cyclone_name: str | None = None
    cyclone_id: int | None = None
    cyclone_type: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus
    center_latitude: float | None = None
    center_longitude: float | None = None
    distance_km: float
    started_at: datetime | None = None
    forecast_waypoints_count: int = 0
    freshness: str


class CycloneAssessmentSearchParameters(SchemaModel):
    lookback_days: int
    search_radius_km: float


class CycloneAssessmentResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    assessment_timestamp: datetime
    assessment_statuses: list[CycloneAssessmentStatus] = Field(default_factory=list)
    primary_status: CycloneAssessmentStatus
    summary_message: str
    search_parameters: CycloneAssessmentSearchParameters
    distance_to_cyclone_center_km: float | None = None
    latest_observed_position: CyclonePosition | None = None
    forecast_track_waypoints: list[CycloneTrackWaypoint] = Field(default_factory=list)
    wind_hazard: CycloneWindHazard
    storm_surge: CycloneStormSurgeHazard
    coastal_inundation: CycloneCoastalHazard
    rainfall_and_inland_flooding: CycloneRainfallFloodHazard
    terrain_and_landslide: CycloneTerrainHazard
    official_warnings: list[CycloneWarningEvidence] = Field(default_factory=list)
    relevant_cyclone_events: list[CycloneEventSummary] = Field(default_factory=list)
    source_attribution: list[DisasterSourceSummary] = Field(default_factory=list)
    contributing_factors: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    data_freshness: str
    scientific_limitations: list[str] = Field(default_factory=list)
