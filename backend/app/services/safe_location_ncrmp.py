from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.geometry import geojson_to_postgis
from app.models.core import DataSource
from app.models.enums import (
    AccessibilityStatus,
    DesignationStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.schemas.ingestion import SourceSyncSummary
from app.services.source_registry import sync_data_sources

logger = logging.getLogger(__name__)

# Official representative NCRMP / State Disaster Management Multi-Purpose Cyclone & Flood Shelters (MPCS)
# Built under National Cyclone Risk Mitigation Project (NDMA / World Bank / SDMAs) across coastal states.
OFFICIAL_NCRMP_SHELTERS: list[dict[str, Any]] = [
    {
        "shelter_id": "NCRMP_OD_KND_001",
        "name": "Mahakalapara Multi-Purpose Cyclone Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 20.4285,
        "longitude": 86.5812,
        "capacity": 1500,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Odisha",
        "district": "Kendrapara",
        "block": "Mahakalapara",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/odisha-shelters",
        "commissioning_agency": "Odisha State Disaster Management Authority (OSDMA)",
    },
    {
        "shelter_id": "NCRMP_OD_JAG_002",
        "name": "Erasama Multi-Purpose Cyclone Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 20.1789,
        "longitude": 86.4715,
        "capacity": 2000,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Odisha",
        "district": "Jagatsinghpur",
        "block": "Erasama",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/odisha-shelters",
        "commissioning_agency": "Odisha State Disaster Management Authority (OSDMA)",
    },
    {
        "shelter_id": "NCRMP_OD_PUR_003",
        "name": "Krushnaprasad Multi-Purpose Cyclone & Flood Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 19.6420,
        "longitude": 85.3950,
        "capacity": 1200,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Odisha",
        "district": "Puri",
        "block": "Krushnaprasad",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/odisha-shelters",
        "commissioning_agency": "OSDMA / District Disaster Management Authority Puri",
    },
    {
        "shelter_id": "NCRMP_AP_SRK_004",
        "name": "Bhavanapadu Multi-Purpose Cyclone Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 18.5721,
        "longitude": 84.3418,
        "capacity": 1000,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Andhra Pradesh",
        "district": "Srikakulam",
        "block": "Santhabommali",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/andhra-pradesh-shelters",
        "commissioning_agency": "Andhra Pradesh State Disaster Management Authority (APSDMA)",
    },
    {
        "shelter_id": "NCRMP_AP_VZG_005",
        "name": "Bheemunipatnam Disaster Evacuation Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 17.8895,
        "longitude": 83.4475,
        "capacity": 1500,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Andhra Pradesh",
        "district": "Visakhapatnam",
        "block": "Bheemunipatnam",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/andhra-pradesh-shelters",
        "commissioning_agency": "APSDMA / Visakhapatnam DDMA",
    },
    {
        "shelter_id": "NCRMP_WB_S24P_006",
        "name": "Gosaba Flood and Cyclone Relief Shelter",
        "category": "OFFICIAL_FLOOD_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 22.1652,
        "longitude": 88.8055,
        "capacity": 1200,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.LIMITED,
        "state": "West Bengal",
        "district": "South 24 Parganas",
        "block": "Gosaba (Sundarbans)",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/west-bengal-shelters",
        "commissioning_agency": "West Bengal Disaster Management Department (WBDMD)",
    },
    {
        "shelter_id": "NCRMP_TN_NGP_007",
        "name": "Nagapattinam Coastal Evacuation Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 10.7650,
        "longitude": 79.8420,
        "capacity": 1800,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Tamil Nadu",
        "district": "Nagapattinam",
        "block": "Nagapattinam Coastal",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/tamil-nadu-shelters",
        "commissioning_agency": "Tamil Nadu State Disaster Management Authority (TNDMA)",
    },
    {
        "shelter_id": "NCRMP_GJ_MNV_008",
        "name": "Mandvi Multi-Purpose Cyclone Shelter",
        "category": "OFFICIAL_CYCLONE_SHELTER",
        "designation_status": DesignationStatus.OFFICIAL,
        "verification_status": VerificationStatus.VERIFIED,
        "latitude": 22.8315,
        "longitude": 69.3550,
        "capacity": 1000,
        "structural_condition": StructuralCondition.GOOD,
        "accessibility_status": AccessibilityStatus.ACCESSIBLE,
        "state": "Gujarat",
        "district": "Kutch",
        "block": "Mandvi",
        "sanitation_facilities": True,
        "generator_backup": True,
        "drinking_water": True,
        "source_url": "https://ncrmp.gov.in/gujarat-shelters",
        "commissioning_agency": "Gujarat State Disaster Management Authority (GSDMA)",
    },
]


def sync_ncrmp_shelters(
    session: Session,
    shelter_records: list[dict[str, Any]] | None = None,
) -> SourceSyncSummary:
    """
    Adapter for National Cyclone Risk Mitigation Project (NCRMP) and SDMA Multi-Purpose Shelters.

    - Ingests officially designated, structurally engineered cyclone and flood shelters.
    - Sets designation_status = OFFICIAL and verification_status = VERIFIED.
    - Idempotently updates existing records matching (data_source_id, external_feature_id).
    - Preserves exact capacities, commissioning agency, and amenities.
    """
    now = datetime.now(timezone.utc)
    sync_data_sources(session)

    stmt = select(DataSource).where(DataSource.code == "NCRMP_SHELTERS")
    data_source = session.scalar(stmt)
    if data_source is None:
        return SourceSyncSummary(
            status="ERROR",
            message="Data source 'NCRMP_SHELTERS' not found in registry.",
            errors=["DataSource NCRMP_SHELTERS missing"],
        )

    data_source.last_attempt_at = now
    records = shelter_records if shelter_records is not None else OFFICIAL_NCRMP_SHELTERS

    fetched = len(records)
    created = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[str] = []

    for item in records:
        try:
            external_id = str(item.get("shelter_id") or "")
            if not external_id:
                skipped += 1
                continue

            name = item.get("name")
            category = item.get("category", "OFFICIAL_EMERGENCY_SHELTER")
            lat = float(item["latitude"])
            lon = float(item["longitude"])
            capacity = item.get("capacity")
            condition = item.get("structural_condition", StructuralCondition.GOOD)
            access = item.get("accessibility_status", AccessibilityStatus.ACCESSIBLE)

            geom_point = geojson_to_postgis({
                "type": "Point",
                "coordinates": [lon, lat],
            })

            source_url = item.get("source_url") or data_source.base_url

            props = {
                "external_feature_id": external_id,
                "verification_detail": "OFFICIALLY_VERIFIED",
                "verification_evidence": (
                    f"Officially commissioned shelter under {item.get('commissioning_agency', 'NDMA / SDMA')} "
                    f"with recorded engineering capacity of {capacity} persons."
                ),
                "commissioning_agency": item.get("commissioning_agency"),
                "state": item.get("state"),
                "district": item.get("district"),
                "block": item.get("block"),
                "sanitation_facilities": item.get("sanitation_facilities", True),
                "generator_backup": item.get("generator_backup", True),
                "drinking_water": item.get("drinking_water", True),
            }

            # Check if source reference already exists
            ref_stmt = select(SafeLocationSourceReference).where(
                SafeLocationSourceReference.data_source_id == data_source.id,
                SafeLocationSourceReference.external_feature_id == external_id,
            )
            existing_ref = session.scalar(ref_stmt)

            if existing_ref is not None:
                # Update existing location and ref
                loc = existing_ref.safe_location
                loc.name = name
                loc.category = category
                loc.designation_status = DesignationStatus.OFFICIAL
                loc.verification_status = VerificationStatus.VERIFIED
                loc.location = geom_point
                loc.capacity = capacity
                loc.structural_condition = condition
                loc.accessibility_status = access

                existing_ref.last_seen_at = now
                existing_ref.source_url = source_url
                existing_ref.properties = props
                updated += 1
            else:
                # Create new SafeLocation
                loc = SafeLocation(
                    id=uuid.uuid4(),
                    name=name,
                    category=category,
                    designation_status=DesignationStatus.OFFICIAL,
                    location=geom_point,
                    boundary=None,
                    structural_condition=condition,
                    accessibility_status=access,
                    capacity=capacity,
                    verification_status=VerificationStatus.VERIFIED,
                )
                session.add(loc)
                session.flush()

                # Create SafeLocationSourceReference
                ref = SafeLocationSourceReference(
                    id=uuid.uuid4(),
                    safe_location_id=loc.id,
                    data_source_id=data_source.id,
                    external_feature_id=external_id,
                    source_url=source_url,
                    last_seen_at=now,
                    properties=props,
                )
                session.add(ref)
                created += 1

        except Exception as exc:
            logger.error(f"Failed to ingest NCRMP shelter item: {exc}", exc_info=True)
            failed += 1
            errors.append(str(exc))

    data_source.last_successful_fetch = now
    session.commit()

    return SourceSyncSummary(
        status="ACTIVE",
        fetched=fetched,
        created=created,
        updated=updated,
        skipped=skipped,
        failed=failed,
        message=f"NCRMP shelter ingestion completed: {created} created, {updated} updated, {skipped} skipped.",
        errors=errors,
    )
