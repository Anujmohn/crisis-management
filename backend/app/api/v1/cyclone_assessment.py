from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.cyclone_assessment import CycloneAssessmentResponse
from app.services.cyclone_assessment import assess_cyclone_hazard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cyclone", tags=["cyclone"])


@router.get(
    "/assessment",
    response_model=CycloneAssessmentResponse,
    summary="Evaluate cyclone geographical hazard around coordinates",
    description=(
        "Assesses cyclone-related geographical hazards around coordinates by independently evaluating: "
        "1. Strong winds (sustained winds, gusts, IMD intensity category, wind warnings). "
        "2. Storm surge (INCOIS coastal water levels, surge estimates, warnings). "
        "3. Coastal inundation (coastal proximity vs inland, wave heights). "
        "4. Heavy rainfall & inland flooding (leveraging existing flood assessment engine). "
        "5. Associated landslide risks in steep terrain under heavy precipitation. "
        "6. Official cyclone and evacuation warnings (IMD, NDMA SACHET). "
        "Distinguishes observed cyclone centers from forecast track waypoints without overwriting observed records. "
        "Missing measurements are strictly preserved as None / null and never converted to 0.0."
    ),
)
def get_cyclone_assessment(
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
            le=2000.0,
            description="Search radius in kilometers (0.1 to 2000.0 km, default 500.0)",
        ),
    ] = 500.0,
    lookback_days: Annotated[
        int,
        Query(
            ge=1,
            le=365,
            description="Lookback window in days (1 to 365, default 7)",
        ),
    ] = 7,
    session: Session = Depends(get_db),
) -> CycloneAssessmentResponse:
    """
    Evaluates cyclone hazard evidence for the specified coordinates.
    """
    return assess_cyclone_hazard(
        session=session,
        latitude=latitude,
        longitude=longitude,
        search_radius_km=search_radius_km,
        lookback_days=lookback_days,
    )
