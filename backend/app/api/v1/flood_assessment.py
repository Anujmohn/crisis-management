from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.flood_assessment import FloodAssessmentResponse
from app.services.flood_assessment import assess_flood_hazard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/flood", tags=["flood"])


@router.get(
    "/assessment",
    response_model=FloodAssessmentResponse,
    summary="Evaluate flood geographical hazard around coordinates",
    description=(
        "Assesses flood-related geographical risks around coordinates by combining official flood observations, "
        "heavy rainfall data (IMD), river level/danger thresholds (CWC), multi-hazard CAP warnings (NDMA SACHET), "
        "and geographical environment susceptibility. "
        "Distinguishes observed flooding from official warnings, rainfall observations, river measurements, "
        "and terrain susceptibility without treating them as interchangeable."
    ),
)
def get_flood_assessment(
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
    search_radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=1000.0,
            description="Search radius in kilometers (0.1 to 1000.0 km)",
        ),
    ] = 50.0,
    lookback_days: Annotated[
        int,
        Query(
            ge=1,
            le=365,
            description="Lookback window in days (1 to 365)",
        ),
    ] = 7,
    session: Session = Depends(get_db),
) -> FloodAssessmentResponse:
    """
    Evaluates flood hazard evidence for the specified coordinates.
    """
    return assess_flood_hazard(
        session=session,
        latitude=latitude,
        longitude=longitude,
        search_radius_km=search_radius_km,
        lookback_days=lookback_days,
    )
