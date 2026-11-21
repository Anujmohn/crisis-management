from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.schemas.ingestion import SourceSyncSummary
from app.schemas.safe_location import SafeLocationSyncResponse
from app.services.safe_location_ncrmp import sync_ncrmp_shelters
from app.services.safe_location_osm import sync_osm_locations

logger = logging.getLogger(__name__)


def sync_all_safe_location_sources(
    session: Session,
    ncrmp_data: list[dict[str, Any]] | None = None,
    osm_data: list[dict[str, Any]] | None = None,
    osm_bbox: tuple[float, float, float, float] | None = None,
) -> SafeLocationSyncResponse:
    """
    Orchestrates synchronization of safe locations across official government shelter registries
    and OpenStreetMap candidate geographical sources.

    - Ingests officially designated shelters via NCRMP / SDMA directories (VERIFIED, OFFICIAL).
    - Ingests candidate facilities and open areas via OpenStreetMap (UNVERIFIED, POTENTIAL).
    - Prevents overwriting of verified official designations.
    - Idempotent execution with clear source-by-source summaries.
    """
    now = datetime.now(timezone.utc)
    summaries: list[SourceSyncSummary] = []

    # 1. Sync official NCRMP government shelters first (establishing baseline verified facilities)
    ncrmp_summary = sync_ncrmp_shelters(session, shelter_records=ncrmp_data)
    summaries.append(ncrmp_summary)

    # 2. Sync OSM candidate geographical features
    osm_summary = sync_osm_locations(session, elements_data=osm_data, bbox=osm_bbox)
    summaries.append(osm_summary)

    total_created = sum(s.created for s in summaries)
    total_updated = sum(s.updated for s in summaries)
    total_skipped = sum(s.skipped for s in summaries)
    total_failed = sum(s.failed for s in summaries)

    return SafeLocationSyncResponse(
        success=all(s.status in ("ACTIVE", "PUBLIC_DOWNLOAD_VERIFIED", "UNAVAILABLE") for s in summaries),
        synced_at=now,
        sources=summaries,
        total_created=total_created,
        total_updated=total_updated,
        total_skipped=total_skipped,
        total_failed=total_failed,
        message=(
            f"Safe location synchronization complete: {total_created} created, "
            f"{total_updated} updated, {total_skipped} skipped across {len(summaries)} sources."
        ),
    )
