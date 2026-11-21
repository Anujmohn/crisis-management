from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.database.geometry import GEOGRAPHIC_SRID, postgis_to_geojson


Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Position: TypeAlias = tuple[Longitude, Latitude]
LinearRing: TypeAlias = tuple[Position, ...]
PolygonCoordinates: TypeAlias = tuple[LinearRing, ...]
MultiPolygonCoordinates: TypeAlias = tuple[PolygonCoordinates, ...]

NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
NonNegativeInteger = Annotated[int, Field(ge=0)]
HazardValue = Annotated[Decimal, Field(max_digits=20, decimal_places=8)]
DistanceMeters = Annotated[
    Decimal,
    Field(ge=0, max_digits=14, decimal_places=3),
]
UnitIntervalScore = Annotated[
    Decimal,
    Field(ge=0, le=1, max_digits=5, decimal_places=4),
]
JsonObject: TypeAlias = dict[str, JsonValue]


class GeoJSONModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeoJSONPoint(GeoJSONModel):
    type: Literal["Point"]
    coordinates: Position


class GeoJSONLineString(GeoJSONModel):
    type: Literal["LineString"]
    coordinates: tuple[Position, ...]

    @field_validator("coordinates")
    @classmethod
    def require_two_positions(cls, value: tuple[Position, ...]) -> tuple[Position, ...]:
        if len(value) < 2:
            raise ValueError("A LineString requires at least two positions")
        return value


def _validate_polygon_coordinates(polygon: PolygonCoordinates) -> None:
    if not polygon:
        raise ValueError("A polygon requires an exterior ring")
    for ring in polygon:
        if len(ring) < 4:
            raise ValueError("A linear ring requires at least four positions")
        if ring[0] != ring[-1]:
            raise ValueError("A linear ring must be closed")


class GeoJSONPolygon(GeoJSONModel):
    type: Literal["Polygon"]
    coordinates: PolygonCoordinates

    @field_validator("coordinates")
    @classmethod
    def validate_rings(cls, value: PolygonCoordinates) -> PolygonCoordinates:
        _validate_polygon_coordinates(value)
        return value


class GeoJSONMultiPolygon(GeoJSONModel):
    type: Literal["MultiPolygon"]
    coordinates: tuple[PolygonCoordinates, ...]

    @field_validator("coordinates")
    @classmethod
    def validate_polygons(
        cls,
        value: tuple[PolygonCoordinates, ...],
    ) -> tuple[PolygonCoordinates, ...]:
        if not value:
            raise ValueError("A MultiPolygon requires at least one polygon")
        for polygon in value:
            _validate_polygon_coordinates(polygon)
        return value


GeoJSONGeometry: TypeAlias = Annotated[
    GeoJSONPoint | GeoJSONLineString | GeoJSONPolygon | GeoJSONMultiPolygon,
    Field(discriminator="type"),
]


class SchemaModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ORMReadSchema(SchemaModel):
    @model_validator(mode="before")
    @classmethod
    def serialize_postgis_geometries(cls, value):
        if isinstance(value, Mapping):
            data = dict(value)
        else:
            data = {
                name: getattr(value, name)
                for name in cls.model_fields
                if hasattr(value, name)
            }

        for name in (
            "location",
            "affected_area",
            "geometry",
            "boundary",
            "user_location",
            "route_geometry",
        ):
            geometry = data.get(name)
            if geometry is None or isinstance(geometry, Mapping):
                continue
            if isinstance(geometry, GeoJSONModel):
                data[name] = geometry.model_dump(mode="json")
            else:
                data[name] = postgis_to_geojson(geometry)
        return data


__all__ = [
    "AwareDatetime",
    "GEOGRAPHIC_SRID",
    "GeoJSONGeometry",
    "GeoJSONLineString",
    "GeoJSONModel",
    "GeoJSONMultiPolygon",
    "GeoJSONPoint",
    "GeoJSONPolygon",
    "DistanceMeters",
    "FiniteFloat",
    "HazardValue",
    "JsonObject",
    "JsonValue",
    "Latitude",
    "LinearRing",
    "Longitude",
    "NonEmptyString",
    "NonNegativeFloat",
    "NonNegativeInteger",
    "ORMReadSchema",
    "PolygonCoordinates",
    "Position",
    "SchemaModel",
    "UnitIntervalScore",
]
