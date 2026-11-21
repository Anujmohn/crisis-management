from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.common import GeoJSONGeometry, GeoJSONMultiPolygon, GeoJSONPoint, SchemaModel


class UserLocation(SchemaModel):
    latitude: float = Field(ge=-90.0, le=90.0, description="User latitude in WGS84 coordinates")
    longitude: float = Field(ge=-180.0, le=180.0, description="User longitude in WGS84 coordinates")


class DisasterSourceSummary(SchemaModel):
    source_name: str
    source_type: str
    source_url: str | None = None
    external_event_id: str | None = None
    issued_at: datetime | None = None
    fetched_at: datetime
    valid_until: datetime | None = None


class DisasterObservationSummary(SchemaModel):
    id: uuid.UUID
    observation_type: str
    value_numeric: Decimal | float | None = None
    value_text: str | None = None
    unit: str | None = None
    geometry: GeoJSONGeometry | None = None
    observed_at: datetime
    properties: dict[str, Any] | None = None


class DisasterEventSummary(SchemaModel):
    id: uuid.UUID
    disaster_type: CrisisType
    title: str | None = None
    description: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus
    location: GeoJSONPoint | None = None
    affected_area: GeoJSONMultiPolygon | None = None
    flow_path: GeoJSONGeometry | None = None
    distance_km: float | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    freshness: str
    sources: list[DisasterSourceSummary] = Field(default_factory=list)
    observations: list[DisasterObservationSummary] = Field(default_factory=list)


class NearbyDisastersResponse(SchemaModel):
    success: bool = True
    user_location: UserLocation
    search_radius_km: float
    total_events: int
    events: list[DisasterEventSummary] = Field(default_factory=list)


__all__ = [
    "DisasterEventSummary",
    "DisasterObservationSummary",
    "DisasterSourceSummary",
    "NearbyDisastersResponse",
    "UserLocation",
]
