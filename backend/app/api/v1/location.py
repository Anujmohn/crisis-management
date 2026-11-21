from fastapi import APIRouter

from app.schemas.location import LocationRequest, LocationValidationResponse
from app.services.location import normalize_location


router = APIRouter(prefix="/api/v1/location", tags=["location"])


@router.post("/validate", response_model=LocationValidationResponse)
def validate_location(
    request: LocationRequest,
) -> LocationValidationResponse:
    return LocationValidationResponse(
        success=True,
        location=normalize_location(request),
    )
