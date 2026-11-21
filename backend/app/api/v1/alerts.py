from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.enums import AlertClassification, CrisisType
from app.schemas.alert import AlertSyncRequest, AlertSyncResponse, NearbyAlertsResponse
from app.services.alert_service import get_nearby_alerts, sync_active_disaster_alerts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get(
    "/nearby",
    response_model=NearbyAlertsResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve active disaster alerts and official warnings near a location",
    description=(
        "Retrieves active official warnings, advisories, and disaster alerts using PostGIS spatial matching. "
        "Evaluates point proximity (ST_DWithin) and warning polygon extents (ST_Intersects). "
        "Preserves original issuing authority, source severity, issue time, and validity period. "
        "Eliminates expired warnings and enforces mandatory non-safety disclaimers."
    ),
)
def get_nearby_alerts_endpoint(
    latitude: Annotated[float, Query(ge=-90.0, le=90.0, description="Latitude in WGS84 coordinates")],
    longitude: Annotated[float, Query(ge=-180.0, le=180.0, description="Longitude in WGS84 coordinates")],
    radius_km: Annotated[float, Query(gt=0.0, le=1000.0, description="Search radius in kilometers")] = 50.0,
    disaster_type: Annotated[CrisisType | None, Query(description="Filter by disaster category")] = None,
    classification: Annotated[AlertClassification | None, Query(description="Filter by alert classification")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum number of alerts to return")] = 50,
    session: Annotated[Session, Depends(get_db)] = None,
) -> NearbyAlertsResponse:
    logger.info(
        "Querying nearby alerts (radius=%.1f km, disaster=%s)",
        radius_km,
        disaster_type.value if disaster_type else "ALL",
    )
    return get_nearby_alerts(
        session=session,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        disaster_type=disaster_type,
        classification=classification,
        limit=limit,
    )


@router.post(
    "/sync",
    response_model=AlertSyncResponse,
    status_code=status.HTTP_200_OK,
    summary="Manually trigger synchronization of verified disaster feeds and generate alerts",
    description=(
        "Executes verified official Indian disaster feeds (NCS, IMD, CWC, INCOIS, SACHET). "
        "Performs payload deduplication, detects new or escalated warnings, dispatches notifications "
        "to registered devices matching spatial preferences, and records monitoring audit logs."
    ),
)
def sync_alerts_endpoint(
    request: AlertSyncRequest | None = None,
    session: Annotated[Session, Depends(get_db)] = None,
) -> AlertSyncResponse:
    disaster_type = request.disaster_type if request else None
    source_code = request.source_code if request else None

    logger.info(
        "Manual alert sync requested (disaster=%s, source=%s)",
        disaster_type.value if disaster_type else "ALL",
        source_code or "ALL",
    )
    return sync_active_disaster_alerts(
        session=session,
        disaster_type=disaster_type,
        source_code=source_code,
        sync_mode="MANUAL",
    )
