from uuid import UUID

from pydantic import AwareDatetime, Field

from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.common import (
    GeoJSONMultiPolygon,
    GeoJSONPoint,
    JsonValue,
    NonEmptyString,
    ORMReadSchema,
    SchemaModel,
)


class CrisisEventCreate(SchemaModel):
    crisis_type: CrisisType
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus = CrisisStatus.UNKNOWN
    location: GeoJSONPoint | None = None
    affected_area: GeoJSONMultiPolygon | None = None
    started_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None


class CrisisEventUpdate(SchemaModel):
    crisis_type: CrisisType = Field(default=None)
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    severity: SeverityLevel | None = None
    status: CrisisStatus = Field(default=None)
    location: GeoJSONPoint | None = None
    affected_area: GeoJSONMultiPolygon | None = None
    started_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    first_seen_at: AwareDatetime = Field(default=None)
    last_seen_at: AwareDatetime = Field(default=None)


class CrisisEventRead(ORMReadSchema):
    id: UUID
    crisis_type: CrisisType
    title: str | None
    description: str | None
    severity: SeverityLevel | None
    status: CrisisStatus
    location: GeoJSONPoint | None
    affected_area: GeoJSONMultiPolygon | None
    started_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CrisisSourceReportCreate(SchemaModel):
    crisis_event_id: UUID
    data_source_id: UUID
    external_event_id: str | None = Field(default=None, max_length=512)
    source_url: str | None = Field(default=None, max_length=2048)
    source_title: str | None = Field(default=None, max_length=500)
    source_description: str | None = None
    source_severity: str | None = Field(default=None, max_length=100)
    source_status: str | None = Field(default=None, max_length=100)
    issued_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    raw_payload: JsonValue | None = None
    payload_hash: str | None = Field(default=None, max_length=128)


class CrisisSourceReportUpdate(SchemaModel):
    crisis_event_id: UUID = Field(default=None)
    data_source_id: UUID = Field(default=None)
    external_event_id: str | None = Field(default=None, max_length=512)
    source_url: str | None = Field(default=None, max_length=2048)
    source_title: str | None = Field(default=None, max_length=500)
    source_description: str | None = None
    source_severity: str | None = Field(default=None, max_length=100)
    source_status: str | None = Field(default=None, max_length=100)
    issued_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    raw_payload: JsonValue | None = None
    payload_hash: str | None = Field(default=None, max_length=128)


class CrisisSourceReportRead(ORMReadSchema):
    id: UUID
    crisis_event_id: UUID
    data_source_id: UUID
    external_event_id: str | None
    source_url: str | None
    source_title: str | None
    source_description: str | None
    source_severity: str | None
    source_status: str | None
    issued_at: AwareDatetime | None
    valid_until: AwareDatetime | None
    fetched_at: AwareDatetime
    raw_payload: JsonValue | None
    payload_hash: str | None


__all__ = [
    "CrisisEventCreate",
    "CrisisEventRead",
    "CrisisEventUpdate",
    "CrisisSourceReportCreate",
    "CrisisSourceReportRead",
    "CrisisSourceReportUpdate",
]
