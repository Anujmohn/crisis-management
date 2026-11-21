from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.enums import VerificationStatus
from app.schemas.safe_location import (
    NearbySafeLocationsResponse,
    SafeLocationDetailResponse,
    SafeLocationSyncResponse,
)
from app.services.safe_location import (
    get_all_safe_locations,
    get_nearby_safe_locations,
    get_safe_location_by_id,
)
from app.services.safe_location_ingestion import sync_all_safe_location_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/safe-locations", tags=["safe-locations"])


@router.get(
    "/nearby",
    response_model=NearbySafeLocationsResponse,
    summary="Retrieve nearby safe locations and candidate evacuation destinations",
    description=(
        "Retrieves geographically referenced shelters, candidate assembly areas, and public facilities "
        "near coordinates using PostGIS spatial indexing and distance ordering. "
        "Distinguishes officially verified emergency shelters from unverified candidate facilities and open areas. "
        "Does not label unverified candidate locations as safe destinations."
    ),
)
def get_nearby_locations(
    latitude: Annotated[
        float,
        Query(
            ge=-90.0,
            le=90.0,
            description="User latitude in WGS84 coordinates (-90.0 to 90.0)",
        ),
    ],
    longitude: Annotated[
        float,
        Query(
            ge=-180.0,
            le=180.0,
            description="User longitude in WGS84 coordinates (-180.0 to 180.0)",
        ),
    ],
    radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=500.0,
            description="Search radius in kilometers (0.1 to 500.0 km, default 25.0)",
        ),
    ] = 25.0,
    category: Annotated[
        str | None,
        Query(
            description=(
                "Optional category filter (e.g. OFFICIAL_CYCLONE_SHELTER, "
                "OFFICIAL_FLOOD_SHELTER, COMMUNITY_CENTRE, SCHOOL, HOSPITAL, PARK, OPEN_GROUND)"
            ),
        ),
    ] = None,
    verification_status: Annotated[
        VerificationStatus | None,
        Query(
            description="Optional verification status filter (VERIFIED, UNVERIFIED, REJECTED)",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=200,
            description="Maximum number of results to return (1 to 200, default 50)",
        ),
    ] = 50,
    session: Session = Depends(get_db),
) -> NearbySafeLocationsResponse:
    """
    Evaluates and returns nearby safe locations and candidate assembly areas.
    """
    return get_nearby_safe_locations(
        session=session,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        category=category,
        verification_status=verification_status,
        limit=limit,
    )


@router.post(
    "/sync",
    response_model=SafeLocationSyncResponse,
    summary="Manually synchronize safe locations and candidate destinations",
    description=(
        "Manually synchronizes safe locations from official government shelter directories "
        "(e.g. NCRMP / SDMA Multi-Purpose Shelters) and OpenStreetMap candidate facilities. "
        "Maintains idempotency and strict verification status separation."
    ),
)
def sync_safe_locations(
    session: Session = Depends(get_db),
) -> SafeLocationSyncResponse:
    """
    Triggers idempotent ingestion for verified shelter directories and geographical candidate sources.
    """
    return sync_all_safe_location_sources(session=session)


@router.get(
    "/all",
    response_model=NearbySafeLocationsResponse,
    summary="Retrieve all safe locations across India",
    description="Returns all registered safe shelters and candidate assembly areas across India.",
)
def get_all_locations(
    category: Annotated[
        str | None,
        Query(
            description=(
                "Optional category filter (e.g. OFFICIAL_CYCLONE_SHELTER, "
                "OFFICIAL_FLOOD_SHELTER, COMMUNITY_CENTRE, SCHOOL, HOSPITAL, PARK, OPEN_GROUND)"
            ),
        ),
    ] = None,
    verification_status: Annotated[
        VerificationStatus | None,
        Query(
            description="Optional verification status filter (VERIFIED, UNVERIFIED, REJECTED)",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Maximum number of results to return (1 to 500, default 200)",
        ),
    ] = 200,
    session: Session = Depends(get_db),
) -> NearbySafeLocationsResponse:
    return get_all_safe_locations(
        session=session,
        category=category,
        verification_status=verification_status,
        limit=limit,
    )


@router.get(
    "/{location_id}",
    response_model=SafeLocationDetailResponse,
    summary="Retrieve individual safe location details and verification evidence",
    description=(
        "Returns comprehensive location details, all attached source references, "
        "verification evidence, structural / capacity records, and disaster suitability limitations."
    ),
)
def get_location_details(
    location_id: uuid.UUID,
    session: Session = Depends(get_db),
) -> SafeLocationDetailResponse:
    """
    Fetches full metadata and verification evidence for a specific location.
    """
    detail = get_safe_location_by_id(session=session, location_id=location_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Safe location with ID {location_id} not found.",
        )
    return detail
