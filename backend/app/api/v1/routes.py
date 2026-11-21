from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.route import (
    CalculateRouteRequest,
    CalculateRouteResponse,
)
from app.services.evacuation_routing import calculate_evacuation_routes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/routes", tags=["routes"])


@router.post(
    "/calculate",
    response_model=CalculateRouteResponse,
    status_code=status.HTTP_200_OK,
    summary="Calculate road evacuation routes and evaluate disaster hazard avoidance",
    description=(
        "Calculates drivable road evacuation routes from user origin to an assessed safe location destination. "
        "Leverages OSRM (Open Source Routing Machine) road network routing and requests alternative routes. "
        "Performs post-processing hazard intersection analysis across earthquake, flood, and cyclone disaster threats. "
        "Strictly excludes routes intersecting active flood inundation or coastal storm surge corridors (ROUTE_EXCLUDED). "
        "Compares alternatives and ranks the path with fewer verified hazard exposures, rejecting shortest routes when hazardous. "
        "Persists the calculated route and hazard intersections in EvacuationRoute table."
    ),
)
def calculate_route(
    request: CalculateRouteRequest,
    session: Annotated[Session, Depends(get_db)],
) -> CalculateRouteResponse:
    logger.info(
        "Calculating evacuation route to assessment=%s (alternatives=%s)",
        request.location_assessment_id,
        request.allow_alternatives,
    )
    return calculate_evacuation_routes(
        session=session,
        origin_latitude=request.origin_latitude,
        origin_longitude=request.origin_longitude,
        location_assessment_id=request.location_assessment_id,
        allow_alternatives=request.allow_alternatives,
    )
