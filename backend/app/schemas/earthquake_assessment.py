from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field

from app.models.enums import CrisisStatus, SeverityLevel
from app.schemas.common import SchemaModel
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation


class DepthClassification(str, Enum):
    SHALLOW = "SHALLOW"
    INTERMEDIATE = "INTERMEDIATE"
    DEEP = "DEEP"
    UNKNOWN = "UNKNOWN"


class AssessmentStatus(str, Enum):
    POTENTIAL_RELEVANT_EVENT = "POTENTIAL_RELEVANT_EVENT"
    NO_RELEVANT_EVENT_FOUND = "NO_RELEVANT_EVENT_FOUND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DATA_STALE = "DATA_STALE"


class AssessedEarthquakeEvent(SchemaModel):
    id: uuid.UUID
    title: str | None = None
    description: str | None = None
    origin_time: datetime | None = None
    magnitude: float | None = None
    magnitude_type: str = "M"
    depth_km: float | None = None
    depth_classification: str
    epicentral_distance_km: float
    hypocentral_distance_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    reported_intensity: str | None = None
    reported_impact: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus
    freshness: str
    sources: list[DisasterSourceSummary] = Field(default_factory=list)
    data_completeness: str  # "COMPLETE" | "PARTIAL" | "MINIMAL"
    event_notes: list[str] = Field(default_factory=list)


class EarthquakeAssessmentSearchParameters(SchemaModel):
    lookback_days: int
    search_radius_km: float


class EarthquakeAssessmentResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    assessment_timestamp: datetime
    assessment_status: str
    summary_message: str
    search_parameters: EarthquakeAssessmentSearchParameters
    events_analyzed_count: int
    relevant_events: list[AssessedEarthquakeEvent] = Field(default_factory=list)
    missing_data_warnings: list[str] = Field(default_factory=list)
    scientific_limitations: list[str] = Field(default_factory=list)
