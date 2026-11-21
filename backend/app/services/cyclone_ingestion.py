from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.schemas.ingestion import CycloneIngestionSyncResponse
from app.services.cyclone_imd import sync_imd_cyclone_data
from app.services.cyclone_incois import sync_incois_coastal_data
from app.services.cyclone_sachet import sync_sachet_cyclone_alerts

logger = logging.getLogger(__name__)


def sync_all_cyclone_sources(
    session: Session,
    max_imd_records: int | None = 10,
    max_sachet_alerts: int | None = 15,
) -> CycloneIngestionSyncResponse:
    """
    Orchestrates cyclone-data ingestion across official Indian government sources:
    1. India Meteorological Department (IMD) / RSMC - Cyclone tracks & bulletins
    2. NDMA SACHET - Multi-hazard CAP cyclone & coastal warnings
    3. Indian National Centre for Ocean Information Services (INCOIS) - Ocean state & storm surge

    Returns a detailed source-wise summary.
    """
    logger.info("Starting cyclone data synchronization across verified sources...")

    # 1. India Meteorological Department (IMD)
    imd_summary = sync_imd_cyclone_data(session=session, max_records=max_imd_records)

    # 2. NDMA SACHET (CAP Alert System)
    sachet_summary = sync_sachet_cyclone_alerts(session=session, max_alerts=max_sachet_alerts)

    # 3. INCOIS (Ocean Information Services)
    incois_summary = sync_incois_coastal_data(session=session)

    # Overall success is True if at least one operational warning feed succeeded
    overall_success = (imd_summary.status == "ACTIVE" or sachet_summary.status == "ACTIVE")

    return CycloneIngestionSyncResponse(
        success=overall_success,
        sources={
            "IMD": imd_summary,
            "SACHET": sachet_summary,
            "INCOIS": incois_summary,
        },
        message="Cyclone data synchronization completed.",
    )
