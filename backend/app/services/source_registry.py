from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import DataSource
from app.models.enums import CrisisType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifiedSourceDefinition:
    """
    Immutable representation of an officially verified Indian disaster data source.
    """

    code: str
    name: str
    source_type: str
    base_url: str
    enabled: bool
    update_interval_seconds: int
    disaster_categories: tuple[CrisisType, ...]
    access_status: str
    documentation_notes: str


# Registry of verified official Indian data sources across Earthquake, Flood, and Cyclone.
# Only sources with verified, documented automated access are enabled.
VERIFIED_SOURCES: tuple[VerifiedSourceDefinition, ...] = (
    VerifiedSourceDefinition(
        code="NDMA_SACHET",
        name="NDMA SACHET - Integrated Early Warning System (CAP)",
        source_type="CAP_RSS",
        base_url="https://sachet.ndma.gov.in",
        enabled=True,
        update_interval_seconds=300,  # 5-minute recommended polling interval for emergency CAP feeds
        disaster_categories=(
            CrisisType.EARTHQUAKE,
            CrisisType.FLOOD,
            CrisisType.CYCLONE,
        ),
        access_status="PUBLIC_API_VERIFIED",
        documentation_notes=(
            "Official All-India CAP 1.2 RSS feed at "
            "https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml. "
            "Alert details retrieved via FetchXMLFile endpoint. "
            "Public access without authentication under ITU Common Alerting Protocol standards."
        ),
    ),
    VerifiedSourceDefinition(
        code="NCS_SEISMO",
        name="National Center for Seismology (NCS)",
        source_type="PUBLIC_PORTAL",
        base_url="https://riseq.seismo.gov.in/riseq/earthquake/recent_earthquake",
        enabled=True,
        update_interval_seconds=300,
        disaster_categories=(CrisisType.EARTHQUAKE,),
        access_status="PUBLIC_WEB_DISSEMINATION",
        documentation_notes=(
            "Nodal agency for earthquake monitoring under MoES. Live real-time earthquake "
            "dissemination table at https://riseq.seismo.gov.in/riseq/earthquake/recent_earthquake. "
            "Publicly accessible over HTTPS with event origin time, epicenter coordinates, depth, "
            "magnitude, regional description, and unique event identifiers."
        ),
    ),
    VerifiedSourceDefinition(
        code="IMD_WEATHER_API",
        name="India Meteorological Department (IMD) Nowcast & Warnings",
        source_type="OGC_WFS_GEOJSON",
        base_url="https://reactjs.imd.gov.in/geoserver/imd/ows",
        enabled=True,
        update_interval_seconds=1800,  # 30-minute nowcast update cycle
        disaster_categories=(CrisisType.CYCLONE, CrisisType.FLOOD),
        access_status="PUBLIC_API_VERIFIED",
        documentation_notes=(
            "Official IMD GeoServer WFS 1.1.0 GeoJSON service at "
            "https://reactjs.imd.gov.in/geoserver/imd/ows (typename=imd:NowcastWarningDistrict). "
            "Publicly accessible without API key. Provides real-time district nowcasts with heavy rainfall, "
            "flash flood risk categories, warning colors, and district MultiPolygon boundaries."
        ),
    ),
    VerifiedSourceDefinition(
        code="CWC_FLOOD",
        name="Central Water Commission (CWC) Flood Forecasting",
        source_type="PUBLIC_HYDROLOGY_PORTAL",
        base_url="https://ffs.india-water.gov.in",
        enabled=False,
        update_interval_seconds=0,
        disaster_categories=(CrisisType.FLOOD,),
        access_status="MANUAL_ACCESS_ONLY",
        documentation_notes=(
            "Flood forecasting portal at https://ffs.india-water.gov.in and advisory flood forecast "
            "at https://aff.india-water.gov.in. No open public developer REST API. NWIC hydrology "
            "telemetry APIs require registration and organizational approval."
        ),
    ),
    VerifiedSourceDefinition(
        code="INCOIS_OCEAN",
        name="Indian National Centre for Ocean Information Services (INCOIS)",
        source_type="OCEAN_DATA_SERVICE",
        base_url="https://erddap.incois.gov.in/erddap",
        enabled=True,
        update_interval_seconds=21600,
        disaster_categories=(CrisisType.CYCLONE,),
        access_status="PUBLIC_DOWNLOAD_VERIFIED",
        documentation_notes=(
            "Provides Storm Surge Early Warning Services (SSEWS), coastal inundation modeling, "
            "and ocean state forecasts. Disseminates bulletins and ERDDAP data at https://erddap.incois.gov.in/erddap. "
            "Operational emergency coastal warnings are syndicated downstream via NDMA SACHET."
        ),
    ),
    VerifiedSourceDefinition(
        code="NRSC_NDEM",
        name="National Remote Sensing Centre (NRSC) - NDEM",
        source_type="RESTRICTED_GIS",
        base_url="https://ndem.nrsc.gov.in",
        enabled=False,
        update_interval_seconds=0,
        disaster_categories=(
            CrisisType.FLOOD,
            CrisisType.CYCLONE,
            CrisisType.EARTHQUAKE,
        ),
        access_status="RESTRICTED_ACCESS",
        documentation_notes=(
            "National Database for Emergency Management by ISRO/NRSC. Multi-scale geospatial disaster "
            "inundation layers and satellite damage assessment. Restricted access strictly requiring "
            "official User Authorization Form endorsement by government authority."
        ),
    ),
    VerifiedSourceDefinition(
        code="NCRMP_SHELTERS",
        name="National Cyclone Risk Mitigation Project (NCRMP) Shelter Registry",
        source_type="GOVERNMENT_SHELTER_DIRECTORY",
        base_url="https://ncrmp.gov.in",
        enabled=True,
        update_interval_seconds=86400,
        disaster_categories=(CrisisType.CYCLONE, CrisisType.FLOOD),
        access_status="OFFICIAL_GOVERNMENT_DIRECTORY",
        documentation_notes=(
            "Official multi-purpose cyclone and flood shelters constructed under NCRMP (NDMA/World Bank) "
            "and State Disaster Management Authorities (OSDMA, APSDMA, TNDMA, GSDMA, WBDMD). "
            "Officially verified emergency evacuation shelters with structural capacity and geographic coordinates."
        ),
    ),
    VerifiedSourceDefinition(
        code="OSM_OVERPASS",
        name="OpenStreetMap Community Geographical Features",
        source_type="OSM_OVERPASS_API",
        base_url="https://overpass-api.de/api/interpreter",
        enabled=True,
        update_interval_seconds=86400,
        disaster_categories=(
            CrisisType.EARTHQUAKE,
            CrisisType.FLOOD,
            CrisisType.CYCLONE,
        ),
        access_status="PUBLIC_API_VERIFIED",
        documentation_notes=(
            "Public Overpass API for querying geographical candidate features (community centres, "
            "schools, hospitals, parks, playgrounds, open grounds). OpenStreetMap is a source of "
            "geographical features, NOT proof of official shelter designation or disaster safety. "
            "All ingested locations are classified as UNVERIFIED candidate facilities."
        ),
    ),
)


def get_verified_sources() -> tuple[VerifiedSourceDefinition, ...]:
    """
    Returns the in-memory tuple of verified source definitions.
    """
    return VERIFIED_SOURCES


def get_verified_source_by_code(code: str) -> VerifiedSourceDefinition | None:
    """
    Retrieves a verified source definition by its unique code.
    """
    for source in VERIFIED_SOURCES:
        if source.code == code:
            return source
    return None


def sync_data_sources(session: Session) -> list[DataSource]:
    """
    Idempotently synchronizes the verified source registry with the database.
    Ensures DataSource records exist for all verified Indian agencies without
    duplicating entries.

    Only verified automated sources are enabled.
    """
    synced_records: list[DataSource] = []

    for definition in VERIFIED_SOURCES:
        stmt = select(DataSource).where(DataSource.code == definition.code)
        existing = session.scalar(stmt)

        if existing is None:
            source = DataSource(
                id=uuid.uuid4(),
                code=definition.code,
                name=definition.name,
                source_type=definition.source_type,
                base_url=definition.base_url,
                enabled=definition.enabled,
                update_interval_seconds=definition.update_interval_seconds,
            )
            session.add(source)
            synced_records.append(source)
            logger.info("Registered new data source: %s (%s)", source.code, source.name)
        else:
            existing.name = definition.name
            existing.source_type = definition.source_type
            existing.base_url = definition.base_url
            existing.enabled = definition.enabled
            existing.update_interval_seconds = definition.update_interval_seconds
            synced_records.append(existing)
            logger.info("Updated existing data source: %s", existing.code)

    session.commit()
    return synced_records


if __name__ == "__main__":
    from app.database.session import SessionLocal

    print("Synchronizing verified Indian disaster data sources to database...")
    with SessionLocal() as db_session:
        results = sync_data_sources(db_session)
        print(f"Successfully synchronized {len(results)} data sources.")
        for src in results:
            status = "ENABLED" if src.enabled else "DISABLED"
            print(f" - [{src.code}] {src.name} (Type: {src.source_type}, Status: {status})")
