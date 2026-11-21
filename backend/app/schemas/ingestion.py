from pydantic import Field

from app.schemas.common import SchemaModel


class IngestionSyncResponse(SchemaModel):
    success: bool
    source: str
    fetched: int = Field(ge=0)
    created: int = Field(ge=0)
    updated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    message: str | None = None
    errors: list[str] = Field(default_factory=list)


class SourceSyncSummary(SchemaModel):
    status: str = "ACTIVE"
    fetched: int = Field(default=0, ge=0)
    created: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    message: str | None = None
    errors: list[str] = Field(default_factory=list)


class FloodIngestionSyncResponse(SchemaModel):
    success: bool
    sources: dict[str, SourceSyncSummary]
    message: str | None = None


class CycloneIngestionSyncResponse(SchemaModel):
    success: bool
    sources: dict[str, SourceSyncSummary]
    message: str | None = None


__all__ = [
    "CycloneIngestionSyncResponse",
    "FloodIngestionSyncResponse",
    "IngestionSyncResponse",
    "SourceSyncSummary",
]
