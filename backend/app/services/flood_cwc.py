from __future__ import annotations

import logging
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import DataSource, HazardObservation
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

CWC_TELEMETRY_PORTAL_URL = "https://ffs.india-water.gov.in/"
CWC_OFFICIAL_URL = "https://cwc.gov.in/"


def probe_cwc_service(url: str = CWC_TELEMETRY_PORTAL_URL, timeout_seconds: float = 8.0) -> tuple[bool, str]:
    """
    Probes the Central Water Commission flood portal availability.
    Returns (is_available, status_message).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "DisasterSafetyApp/1.0 (+https://github.com/disaster-safety-app)"},
    )

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_seconds) as resp:
            if resp.status == 200:
                return True, "CWC portal reachable."
            return False, f"CWC portal returned HTTP status {resp.status}."
    except urllib.error.HTTPError as exc:
        return False, f"CWC portal HTTP Error {exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"CWC portal connection failed: {exc}"


def sync_cwc_flood_data(
    session: Session,
    observations_data: list[dict[str, Any]] | None = None,
    probe_url: str = CWC_TELEMETRY_PORTAL_URL,
) -> SourceSyncSummary:
    """
    Adapter for Central Water Commission (CWC) river flood data.

    - Verifies portal accessibility.
    - If automated live endpoint is unavailable (e.g. HTTP 502 / manual only),
      truthfully reports status without pretending success.
    - Ingests valid station water level records into HazardObservation when provided.
    - Ensures idempotency via station_id + observed_at matching.
    """
    now = datetime.now(timezone.utc)

    # 1. Ensure DataSource entry exists in DB
    sync_data_sources(session)
    stmt = select(DataSource).where(DataSource.code == "CWC_FLOOD")
    data_source = session.scalar(stmt)
    if data_source is None:
        return SourceSyncSummary(
            status="ERROR",
            message="Data source 'CWC_FLOOD' not found in registry.",
            errors=["DataSource CWC_FLOOD missing"],
        )

    data_source.last_attempt_at = now

    # 2. If no data provided directly, probe live CWC portal
    if observations_data is None:
        is_live, probe_msg = probe_cwc_service(url=probe_url)
        if not is_live:
            session.commit()
            return SourceSyncSummary(
                status="UNAVAILABLE",
                message=(
                    f"Central Water Commission telemetry service unavailable: {probe_msg}. "
                    "Public REST API is undocumented; automated telemetry requires NWIC authorization."
                ),
                errors=[probe_msg],
            )
        # If reachable but returns raw unparsed portal HTML without open JSON API
        session.commit()
        return SourceSyncSummary(
            status="MANUAL_ACCESS_ONLY",
            message=(
                "CWC web portal is online, but does not provide an open public JSON/REST endpoint. "
                "Hydrology telemetry data requires formal NWIC credentials."
            ),
        )

    # 3. Process provided observations (e.g. from NWIC API payload or test data)
    created_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    for item in observations_data:
        try:
            station_code = item.get("station_code") or item.get("station_id")
            if not station_code:
                failed_count += 1
                errors.append("Observation missing station_code.")
                continue

            # Coordinates
            lat = item.get("latitude")
            lon = item.get("longitude")
            geom = None
            if lat is not None and lon is not None:
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                    if -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0:
                        geom = geojson_to_postgis({"type": "Point", "coordinates": [lon_f, lat_f]})
                except (ValueError, TypeError):
                    pass

            # Timestamp
            observed_at_raw = item.get("observed_at")
            if isinstance(observed_at_raw, datetime):
                observed_at = observed_at_raw if observed_at_raw.tzinfo else observed_at_raw.replace(tzinfo=timezone.utc)
            elif isinstance(observed_at_raw, str):
                try:
                    observed_at = datetime.fromisoformat(observed_at_raw)
                    if not observed_at.tzinfo:
                        observed_at = observed_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    failed_count += 1
                    errors.append(f"Invalid timestamp format '{observed_at_raw}' for station {station_code}.")
                    continue
            else:
                observed_at = now

            obs_type = item.get("observation_type", "RIVER_WATER_LEVEL")
            val_num = item.get("water_level") or item.get("value_numeric")
            value_numeric = Decimal(str(val_num)) if val_num is not None else None
            unit = item.get("unit", "m")
            val_text = item.get("value_text") or (f"{val_num} {unit}" if val_num is not None else None)

            properties = {
                "station_code": str(station_code),
                "station_name": item.get("station_name"),
                "river_name": item.get("river_name"),
                "basin": item.get("basin"),
                "warning_level_m": item.get("warning_level"),
                "danger_level_m": item.get("danger_level"),
                "hfl_m": item.get("hfl"),
            }

            # Check if observation already exists for this station and timestamp
            obs_stmt = select(HazardObservation).where(
                HazardObservation.data_source_id == data_source.id,
                HazardObservation.observation_type == obs_type,
                HazardObservation.observed_at == observed_at,
            )
            existing_obs_list = session.scalars(obs_stmt).all()
            matching_obs = next(
                (o for o in existing_obs_list if o.properties and o.properties.get("station_code") == str(station_code)),
                None,
            )

            if matching_obs is not None:
                # Update if numeric value changed
                if matching_obs.value_numeric != value_numeric:
                    matching_obs.value_numeric = value_numeric
                    matching_obs.value_text = val_text
                    matching_obs.properties = properties
                    updated_count += 1
                else:
                    skipped_count += 1
            else:
                new_obs = HazardObservation(
                    id=uuid.uuid4(),
                    data_source_id=data_source.id,
                    crisis_event_id=None,
                    observation_type=obs_type,
                    value_numeric=value_numeric,
                    value_text=val_text,
                    unit=unit,
                    geometry=geom,
                    observed_at=observed_at,
                    ingested_at=now,
                    properties=properties,
                )
                session.add(new_obs)
                created_count += 1

        except Exception as exc:
            logger.exception("Error processing CWC observation: %s", exc)
            failed_count += 1
            errors.append(str(exc))

    data_source.last_successful_fetch = now
    session.commit()

    return SourceSyncSummary(
        status="ACTIVE",
        fetched=len(observations_data),
        created=created_count,
        updated=updated_count,
        skipped=skipped_count,
        failed=failed_count,
        message=f"Processed {len(observations_data)} CWC flood observations.",
        errors=errors[:10],
    )
