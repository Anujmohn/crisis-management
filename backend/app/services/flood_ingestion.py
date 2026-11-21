from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.schemas.ingestion import FloodIngestionSyncResponse
from app.services.flood_cwc import sync_cwc_flood_data
from app.services.flood_imd import sync_imd_flood_data
from app.services.flood_sachet import sync_sachet_flood_alerts

logger = logging.getLogger(__name__)


def sync_all_flood_sources(
    session: Session,
    max_imd_records: int | None = 200,
    max_sachet_alerts: int | None = 15,
) -> FloodIngestionSyncResponse:
    """
    Orchestrates flood-data ingestion across official Indian government sources:
    1. Central Water Commission (CWC) - River water levels & flood monitoring
    2. India Meteorological Department (IMD) - Heavy rainfall & flash flood nowcasts
    3. NDMA SACHET - Multi-hazard CAP flood alerts

    Returns a detailed source-wise summary.
    """
    logger.info("Starting flood data synchronization across verified sources...")

    # 1. Central Water Commission (CWC)
    cwc_summary = sync_cwc_flood_data(session=session)

    # 2. India Meteorological Department (IMD)
    imd_summary = sync_imd_flood_data(session=session, max_records=max_imd_records)

    # 3. NDMA SACHET (CAP Alert System)
    sachet_summary = sync_sachet_flood_alerts(session=session, max_alerts=max_sachet_alerts)

    # Overall success is True if at least one active source succeeded without critical crash
    overall_success = (imd_summary.status == "ACTIVE" or sachet_summary.status == "ACTIVE")

    return FloodIngestionSyncResponse(
        success=overall_success,
        sources={
            "CWC": cwc_summary,
            "IMD": imd_summary,
            "NDMA_SACHET": sachet_summary,
        },
        message="Flood data synchronization completed.",
    )
