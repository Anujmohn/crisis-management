from pydantic import AwareDatetime

from app.schemas.common import (
    GeoJSONPoint,
    Latitude,
    Longitude,
    NonNegativeFloat,
    FiniteFloat,
    SchemaModel,
)


class LocationRequest(SchemaModel):
    latitude: Latitude
    longitude: Longitude
    accuracy_m: NonNegativeFloat | None = None
    altitude_m: FiniteFloat | None = None
    captured_at: AwareDatetime


class LocationData(SchemaModel):
    latitude: Latitude
    longitude: Longitude
    accuracy_m: NonNegativeFloat | None
    altitude_m: FiniteFloat | None
    captured_at: AwareDatetime
    geometry: GeoJSONPoint


class LocationValidationResponse(SchemaModel):
    success: bool
    location: LocationData


__all__ = ["LocationData", "LocationRequest", "LocationValidationResponse"]
