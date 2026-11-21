from uuid import UUID

from pydantic import AwareDatetime, Field

from app.schemas.common import (
    NonEmptyString,
    NonNegativeInteger,
    ORMReadSchema,
    SchemaModel,
)


class DataSourceCreate(SchemaModel):
    code: NonEmptyString = Field(max_length=100)
    name: NonEmptyString = Field(max_length=255)
    source_type: NonEmptyString = Field(max_length=80)
    base_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    update_interval_seconds: NonNegativeInteger
    last_attempt_at: AwareDatetime | None = None
    last_successful_fetch: AwareDatetime | None = None


class DataSourceUpdate(SchemaModel):
    code: NonEmptyString = Field(default=None, max_length=100)
    name: NonEmptyString = Field(default=None, max_length=255)
    source_type: NonEmptyString = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = Field(default=None)
    update_interval_seconds: NonNegativeInteger = Field(default=None)
    last_attempt_at: AwareDatetime | None = None
    last_successful_fetch: AwareDatetime | None = None


class DataSourceRead(ORMReadSchema):
    id: UUID
    code: NonEmptyString
    name: NonEmptyString
    source_type: NonEmptyString
    base_url: str | None
    enabled: bool
    update_interval_seconds: NonNegativeInteger
    last_attempt_at: AwareDatetime | None
    last_successful_fetch: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


__all__ = ["DataSourceCreate", "DataSourceRead", "DataSourceUpdate"]
