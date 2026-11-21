from app.schemas.common import GeoJSONPoint
from app.schemas.location import LocationData, LocationRequest


def normalize_location(request: LocationRequest) -> LocationData:
    return LocationData(
        latitude=request.latitude,
        longitude=request.longitude,
        accuracy_m=request.accuracy_m,
        altitude_m=request.altitude_m,
        captured_at=request.captured_at,
        geometry=GeoJSONPoint(
            type="Point",
            coordinates=(request.longitude, request.latitude),
        ),
    )
