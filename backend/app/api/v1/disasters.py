from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.enums import CrisisStatus, CrisisType
from app.schemas.disaster_search import NearbyDisastersResponse
from app.services.disaster_search import get_all_active_disasters, get_nearby_disasters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/disasters", tags=["disasters"])


def parse_disaster_types(raw_types: str | None) -> list[CrisisType] | None:
    """
    Parses and validates a comma-separated list of disaster type strings into CrisisType enums.
    Raises HTTPException 400 if any value is invalid.
    """
    if not raw_types or not raw_types.strip():
        return None

    valid_types = {member.value: member for member in CrisisType}
    parsed: list[CrisisType] = []

    for item in raw_types.split(","):
        cleaned = item.strip().upper()
        if not cleaned:
            continue
        if cleaned not in valid_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid disaster type '{item.strip()}'. "
                    f"Supported types are: {', '.join(sorted(valid_types.keys()))}."
                ),
            )
        parsed.append(valid_types[cleaned])

    return parsed or None


def parse_crisis_statuses(raw_statuses: str | None) -> list[CrisisStatus] | None:
    """
    Parses and validates a comma-separated list of status strings into CrisisStatus enums.
    Raises HTTPException 400 if any value is invalid.
    """
    if not raw_statuses or not raw_statuses.strip():
        return None

    valid_statuses = {member.value: member for member in CrisisStatus}
    parsed: list[CrisisStatus] = []

    for item in raw_statuses.split(","):
        cleaned = item.strip().upper()
        if not cleaned:
            continue
        if cleaned not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid status '{item.strip()}'. "
                    f"Supported statuses are: {', '.join(sorted(valid_statuses.keys()))}."
                ),
            )
        parsed.append(valid_statuses[cleaned])

    return parsed or None


@router.get(
    "/nearby",
    response_model=NearbyDisastersResponse,
    summary="Retrieve nearby verified disaster events (Earthquake, Flood, Cyclone)",
)
def get_nearby_disaster_events(
    latitude: Annotated[
        float,
        Query(
            ge=-90.0,
            le=90.0,
            description="User latitude in WGS84 coordinates (-90.0 to 90.0).",
        ),
    ],
    longitude: Annotated[
        float,
        Query(
            ge=-180.0,
            le=180.0,
            description="User longitude in WGS84 coordinates (-180.0 to 180.0).",
        ),
    ],
    radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=1000.0,
            description="Geographical search radius in kilometers (must be > 0 and <= 1000 km).",
        ),
    ] = 50.0,
    disaster_types: Annotated[
        str | None,
        Query(
            description="Filter by disaster types, comma-separated (e.g. 'EARTHQUAKE,FLOOD,CYCLONE').",
        ),
    ] = None,
    status: Annotated[
        str | None,
        Query(
            description="Filter by crisis status, comma-separated (e.g. 'ACTIVE,EXPIRED').",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=200,
            description="Maximum number of disaster records to return (1 to 200).",
        ),
    ] = 50,
    db: Session = Depends(get_db),
) -> NearbyDisastersResponse:
    """
    Retrieves nearby verified disaster events from the PostgreSQL/PostGIS spatial database.

    - Computes metric geodesic distance between user location and disaster events.
    - Evaluates distance to point epicenters and intersection with affected polygon corridors.
    - Preserves source provenance, bulletin references, and freshness indicators.
    - Embeds relevant physical measurements (magnitude, wind speed, rainfall intensity).
    - Returns standardized GeoJSON geometry representations.
    """
    type_filters = parse_disaster_types(disaster_types)
    status_filters = parse_crisis_statuses(status)

    return get_nearby_disasters(
        session=db,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        disaster_types=type_filters,
        statuses=status_filters,
        limit=limit,
    )


@router.get(
    "/active",
    response_model=NearbyDisastersResponse,
    summary="Retrieve all current active disaster events across India",
    description="Retrieves verified ongoing natural disaster records occurring across India at the current time.",
)
@router.get(
    "/all",
    response_model=NearbyDisastersResponse,
    summary="Retrieve all current active disaster events across India",
    include_in_schema=False,
)
def get_active_disaster_events(
    disaster_types: Annotated[
        str | None,
        Query(
            description="Filter by disaster types, comma-separated (e.g. 'EARTHQUAKE,FLOOD,CYCLONE').",
        ),
    ] = None,
    status: Annotated[
        str | None,
        Query(
            description="Filter by crisis status, comma-separated (e.g. 'ACTIVE,EXPIRED').",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Maximum number of disaster records to return (1 to 500).",
        ),
    ] = 200,
    db: Session = Depends(get_db),
) -> NearbyDisastersResponse:
    type_filters = parse_disaster_types(disaster_types)
    status_filters = parse_crisis_statuses(status)

    return get_all_active_disasters(
        session=db,
        disaster_types=type_filters,
        statuses=status_filters,
        limit=limit,
    )

