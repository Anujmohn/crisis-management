from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.recommendation import (
    AssessRecommendationsRequest,
    AssessRecommendationsResponse,
)
from app.schemas.unified_recommendation import (
    UnifiedRecommendationRequest,
    UnifiedRecommendationResponse,
)
from app.services.recommendation_engine import assess_safe_locations_suitability
from app.services.unified_recommendation import generate_unified_recommendations

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.post(
    "/assess",
    response_model=AssessRecommendationsResponse,
    status_code=status.HTTP_200_OK,
    summary="Assess disaster-specific suitability of nearby safe locations",
    description=(
        "Evaluates candidate safe locations and shelters against disaster-specific physical hazard criteria. "
        "Differentiates requirements across Earthquake, Flood, and Cyclone disasters. "
        "Enforces hard exclusions (active inundation, mountain flash-flood corridors, coastal open surge zones) "
        "with a 0.0 suitability score and explicit physical reasons. "
        "Classifies locations into SUITABILITY_SUPPORTED_BY_EVIDENCE, CONDITIONALLY_SUITABLE, "
        "NOT_RECOMMENDED, EXCLUDED, and INSUFFICIENT_DATA. "
        "Persists the assessment in RecommendationRun and LocationAssessment audit tables."
    ),
)
def assess_recommendations(
    request: AssessRecommendationsRequest,
    session: Annotated[Session, Depends(get_db)],
) -> AssessRecommendationsResponse:
    logger.info(
        "Assessing safe locations suitability for disaster=%s (radius=%.1f km)",
        request.disaster_type.value,
        request.search_radius_km,
    )
    return assess_safe_locations_suitability(
        session=session,
        latitude=request.latitude,
        longitude=request.longitude,
        disaster_type=request.disaster_type,
        crisis_event_id=request.crisis_event_id,
        search_radius_km=request.search_radius_km,
        limit=request.limit,
    )


@router.post(
    "/unified",
    response_model=UnifiedRecommendationResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate unified disaster recommendations with destinations and evacuation routes",
    description=(
        "Unified Disaster Recommendation Engine that integrates nearby disaster detection, "
        "hazard assessment (Earthquake, Flood, Cyclone), candidate destination discovery, "
        "evidence-based physical suitability evaluation, OSRM evacuation route calculation, "
        "and hazard avoidance into a single coherent workflow. "
        "Applies strict hard exclusions before ranking, prioritizes verified shelter designation "
        "and safe road routes ahead of proximity, provides transparent ranking rationales, "
        "and enforces non-safety disclaimers."
    ),
)
def get_unified_recommendations(
    request: UnifiedRecommendationRequest,
    session: Annotated[Session, Depends(get_db)],
) -> UnifiedRecommendationResponse:
    logger.info(
        "Generating unified recommendations for disaster=%s (radius=%.1f km, max_dest=%d)",
        request.disaster_type.value,
        request.search_radius_km,
        request.max_destinations,
    )
    return generate_unified_recommendations(
        session=session,
        latitude=request.latitude,
        longitude=request.longitude,
        disaster_type=request.disaster_type,
        crisis_event_id=request.crisis_event_id,
        search_radius_km=request.search_radius_km,
        max_destinations=request.max_destinations,
    )

