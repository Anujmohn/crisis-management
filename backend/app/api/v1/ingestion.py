from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.ingestion import (
    CycloneIngestionSyncResponse,
    FloodIngestionSyncResponse,
    IngestionSyncResponse,
)
from app.services.cyclone_ingestion import sync_all_cyclone_sources
from app.services.earthquake_ingestion import sync_earthquakes_from_ncs
from app.services.flood_ingestion import sync_all_flood_sources

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])


@router.post(
    "/earthquakes/sync",
    response_model=IngestionSyncResponse,
    summary="Trigger earthquake data ingestion from National Center for Seismology (NCS)",
)
def sync_earthquakes(
    limit: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Maximum number of recent earthquake records to ingest from NCS.",
    ),
    db: Session = Depends(get_db),
) -> IngestionSyncResponse:
    """
    Triggers an on-demand earthquake ingestion run from the official
    National Center for Seismology (NCS) portal.

    - Fetches the latest reviewed and preliminary earthquake events.
    - Validates coordinates, origin times, and magnitudes.
    - Normalizes records into CrisisEvent and CrisisSourceReport.
    - Idempotently prevents duplicate records using external event identifiers.
    - Preserves full provenance and raw payloads.
    """
    return sync_earthquakes_from_ncs(session=db, max_records=limit)


@router.post(
    "/floods/sync",
    response_model=FloodIngestionSyncResponse,
    summary="Trigger flood data ingestion from official Indian sources (CWC, IMD, SACHET)",
)
def sync_floods(
    max_imd_records: int = Query(
        default=200,
        ge=1,
        le=1000,
        description="Maximum number of district nowcast records to process from IMD.",
    ),
    max_sachet_alerts: int = Query(
        default=15,
        ge=1,
        le=100,
        description="Maximum number of flood alerts to process from NDMA SACHET.",
    ),
    db: Session = Depends(get_db),
) -> FloodIngestionSyncResponse:
    """
    Triggers an on-demand flood and rainfall hazard ingestion run across verified sources:
    - Central Water Commission (CWC) telemetry probe / observations
    - India Meteorological Department (IMD) heavy rainfall & flash flood nowcasts
    - NDMA SACHET Common Alerting Protocol (CAP) flood warnings

    Normalizes data into CrisisEvent, CrisisSourceReport, and HazardObservation.
    Enforces idempotency and provenance tracking.
    """
    return sync_all_flood_sources(
        session=db,
        max_imd_records=max_imd_records,
        max_sachet_alerts=max_sachet_alerts,
    )


@router.post(
    "/cyclones/sync",
    response_model=CycloneIngestionSyncResponse,
    summary="Trigger cyclone data ingestion from official Indian sources (IMD, SACHET, INCOIS)",
)
def sync_cyclones(
    max_imd_records: int = Query(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of cyclone track features to process from IMD.",
    ),
    max_sachet_alerts: int = Query(
        default=15,
        ge=1,
        le=100,
        description="Maximum number of cyclone alerts to process from NDMA SACHET.",
    ),
    db: Session = Depends(get_db),
) -> CycloneIngestionSyncResponse:
    """
    Triggers an on-demand cyclone and coastal hazard ingestion run across verified sources:
    - India Meteorological Department (IMD) / RSMC cyclone tracks & bulletins
    - NDMA SACHET Common Alerting Protocol (CAP) cyclone & coastal warnings
    - Indian National Centre for Ocean Information Services (INCOIS) ocean state & storm surge

    Normalizes data into CrisisEvent, CrisisSourceReport, and HazardObservation.
    Enforces idempotency, preserves forecast track waypoints, and tracks full source provenance.
    """
    return sync_all_cyclone_sources(
        session=db,
        max_imd_records=max_imd_records,
        max_sachet_alerts=max_sachet_alerts,
    )
