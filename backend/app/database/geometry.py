from collections.abc import Mapping
from typing import Any

from geoalchemy2.elements import WKBElement, WKTElement
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel
from shapely.geometry import mapping, shape


GEOGRAPHIC_SRID = 4326
_SUPPORTED_GEOJSON_TYPES = {"Point", "Polygon", "MultiPolygon", "LineString"}


def geojson_to_postgis(geometry: BaseModel | Mapping[str, Any]) -> WKBElement:
    if isinstance(geometry, BaseModel):
        geojson = geometry.model_dump(mode="json")
    elif isinstance(geometry, Mapping):
        geojson = dict(geometry)
    else:
        raise TypeError("geometry must be a validated GeoJSON object")

    if geojson.get("type") not in _SUPPORTED_GEOJSON_TYPES:
        raise ValueError("unsupported GeoJSON geometry type")

    return from_shape(shape(geojson), srid=GEOGRAPHIC_SRID)


def postgis_to_geojson(geometry: WKBElement | WKTElement) -> dict[str, Any]:
    if geometry.srid != GEOGRAPHIC_SRID:
        raise ValueError("PostGIS geometry must use SRID 4326")

    result = mapping(to_shape(geometry))
    if result.get("type") not in _SUPPORTED_GEOJSON_TYPES:
        raise ValueError("unsupported PostGIS geometry type")
    return result
