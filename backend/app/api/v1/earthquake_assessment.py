from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.earthquake_assessment import EarthquakeAssessmentResponse
from app.services.earthquake_assessment import assess_earthquake_hazard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/earthquake", tags=["earthquake"])


@router.get(
    "/assessment",
    response_model=EarthquakeAssessmentResponse,
    summary="Evaluate earthquake geographical hazard around coordinates",
    description=(
        "Assesses recorded earthquake events from verified official seismological feeds (NCS) "
        "around the provided geographical coordinates. Analyzes epicentral and hypocentral distances, "
        "magnitude, focal depth, occurrence time, and data freshness. "
        "Explicitly disclaims prediction and structural safety."
    ),
)
def get_earthquake_assessment(
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
    lookback_days: Annotated[
        int,
        Query(
            ge=1,
            le=365,
            description="Lookback window in days (1 to 365)",
        ),
    ] = 30,
    search_radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=1000.0,
            description="Search radius in kilometers (0.1 to 1000.0 km)",
        ),
    ] = 300.0,
    session: Session = Depends(get_db),
) -> EarthquakeAssessmentResponse:
    """
    Evaluates earthquake hazard evidence for the specified coordinates.
    """
    return assess_earthquake_hazard(
        session=session,
        latitude=latitude,
        longitude=longitude,
        lookback_days=lookback_days,
        search_radius_km=search_radius_km,
    )
