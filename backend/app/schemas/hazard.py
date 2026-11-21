from uuid import UUID

from pydantic import AwareDatetime, Field

from app.schemas.common import (
    HazardValue,
    GeoJSONGeometry,
    JsonObject,
    NonEmptyString,
    ORMReadSchema,
    SchemaModel,
)


class HazardObservationCreate(SchemaModel):
    crisis_event_id: UUID | None = None
    data_source_id: UUID
    observation_type: NonEmptyString = Field(max_length=100)
    value_numeric: HazardValue | None = None
    value_text: str | None = None
    unit: str | None = Field(default=None, max_length=64)
    geometry: GeoJSONGeometry | None = None
    observed_at: AwareDatetime
    properties: JsonObject | None = None


class HazardObservationUpdate(SchemaModel):
    crisis_event_id: UUID | None = None
    data_source_id: UUID = Field(default=None)
    observation_type: NonEmptyString = Field(default=None, max_length=100)
    value_numeric: HazardValue | None = None
    value_text: str | None = None
    unit: str | None = Field(default=None, max_length=64)
    geometry: GeoJSONGeometry | None = None
    observed_at: AwareDatetime = Field(default=None)
    properties: JsonObject | None = None


class HazardObservationRead(ORMReadSchema):
    id: UUID
    crisis_event_id: UUID | None
    data_source_id: UUID
    observation_type: NonEmptyString
    value_numeric: HazardValue | None
    value_text: str | None
    unit: str | None
    geometry: GeoJSONGeometry | None
    observed_at: AwareDatetime
    ingested_at: AwareDatetime
    properties: JsonObject | None


__all__ = [
    "HazardObservationCreate",
    "HazardObservationRead",
    "HazardObservationUpdate",
]
