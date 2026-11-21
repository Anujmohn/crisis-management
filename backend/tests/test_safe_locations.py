from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.safe_locations import (
    get_location_details,
    get_nearby_locations,
    sync_safe_locations,
)
from app.main import app
from app.models.core import DataSource
from app.models.enums import (
    AccessibilityStatus,
    DesignationStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.schemas.ingestion import SourceSyncSummary
from app.schemas.safe_location import (
    NearbySafeLocationsResponse,
    SafeLocationDetailResponse,
    SafeLocationSyncResponse,
)
from app.services.safe_location import (
    format_safe_location_item,
    get_nearby_safe_locations,
    get_safe_location_by_id,
)
from app.services.safe_location_ingestion import sync_all_safe_location_sources
from app.services.safe_location_ncrmp import (
    OFFICIAL_NCRMP_SHELTERS,
    sync_ncrmp_shelters,
)
from app.services.safe_location_osm import (
    map_osm_tags_to_category,
    parse_osm_accessibility,
    sync_osm_locations,
)


class SafeLocationTests(unittest.TestCase):
    def test_safe_location_routes_registered(self) -> None:
        """Verifies OpenAPI schema registers all safe location endpoints with correct parameter constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})

        # 1. Nearby endpoint
        self.assertIn("/api/v1/safe-locations/nearby", paths)
        nearby_op = paths["/api/v1/safe-locations/nearby"]["get"]
        self.assertIsNotNone(nearby_op)

        params = {p["name"]: p for p in nearby_op["parameters"]}
        self.assertIn("latitude", params)
        self.assertIn("longitude", params)
        self.assertIn("radius_km", params)
        self.assertIn("category", params)
        self.assertIn("verification_status", params)
        self.assertIn("limit", params)

        self.assertEqual(params["latitude"]["schema"].get("minimum"), -90.0)
        self.assertEqual(params["latitude"]["schema"].get("maximum"), 90.0)
        self.assertTrue(params["latitude"]["required"])

        self.assertEqual(params["longitude"]["schema"].get("minimum"), -180.0)
        self.assertEqual(params["longitude"]["schema"].get("maximum"), 180.0)
        self.assertTrue(params["longitude"]["required"])

        self.assertEqual(params["radius_km"]["schema"].get("default"), 25.0)
        self.assertEqual(params["radius_km"]["schema"].get("maximum"), 500.0)

        self.assertEqual(params["limit"]["schema"].get("default"), 50)
        self.assertEqual(params["limit"]["schema"].get("maximum"), 200)

        # 2. Sync endpoint
        self.assertIn("/api/v1/safe-locations/sync", paths)
        self.assertIn("post", paths["/api/v1/safe-locations/sync"])

        # 3. Individual location details endpoint
        self.assertIn("/api/v1/safe-locations/{location_id}", paths)
        self.assertIn("get", paths["/api/v1/safe-locations/{location_id}"])

    def test_osm_tag_mapping_and_accessibility(self) -> None:
        """Verifies mapping of OSM tags to categories and accessibility values."""
        self.assertEqual(map_osm_tags_to_category({"leisure": "park"}), "PARK")
        self.assertEqual(map_osm_tags_to_category({"leisure": "pitch"}), "PLAYGROUND")
        self.assertEqual(map_osm_tags_to_category({"leisure": "common"}), "OPEN_GROUND")
        self.assertEqual(map_osm_tags_to_category({"amenity": "community_centre"}), "COMMUNITY_CENTRE")
        self.assertEqual(map_osm_tags_to_category({"amenity": "school"}), "SCHOOL")
        self.assertEqual(map_osm_tags_to_category({"amenity": "hospital"}), "HOSPITAL")
        self.assertEqual(map_osm_tags_to_category({"amenity": "shelter"}), "OTHER_CANDIDATE")
        self.assertEqual(map_osm_tags_to_category({"building": "yes"}), "OTHER_CANDIDATE")

        self.assertEqual(parse_osm_accessibility({"wheelchair": "yes"}), AccessibilityStatus.ACCESSIBLE)
        self.assertEqual(parse_osm_accessibility({"wheelchair": "limited"}), AccessibilityStatus.LIMITED)
        self.assertEqual(parse_osm_accessibility({"wheelchair": "no"}), AccessibilityStatus.INACCESSIBLE)
        self.assertIsNone(parse_osm_accessibility({}))

    @patch("app.services.safe_location_ncrmp.sync_data_sources")
    def test_ncrmp_shelter_adapter_ingests_officially_verified_shelters(self, mock_sync_ds: MagicMock) -> None:
        """Verifies NCRMP adapter marks shelters as OFFICIAL and VERIFIED with recorded capacity."""
        mock_ds = MagicMock(spec=DataSource)
        mock_ds.id = uuid.uuid4()
        mock_ds.code = "NCRMP_SHELTERS"
        mock_ds.base_url = "https://ncrmp.gov.in"

        mock_session = MagicMock()
        # First call: DataSource query; next calls: ref_stmt queries (returns None for new)
        mock_session.scalar.side_effect = [mock_ds, None, None]

        test_records = [
            {
                "shelter_id": "TEST_OD_001",
                "name": "Kendrapara Multi-Purpose Cyclone Shelter",
                "category": "OFFICIAL_CYCLONE_SHELTER",
                "latitude": 20.4285,
                "longitude": 86.5812,
                "capacity": 1500,
                "structural_condition": StructuralCondition.GOOD,
                "accessibility_status": AccessibilityStatus.ACCESSIBLE,
                "commissioning_agency": "OSDMA",
            },
            {
                "shelter_id": "TEST_OD_002",
                "name": "Puri Coastal Multi-Purpose Shelter",
                "category": "OFFICIAL_CYCLONE_SHELTER",
                "latitude": 19.6420,
                "longitude": 85.3950,
                "capacity": 1200,
                "structural_condition": StructuralCondition.GOOD,
                "accessibility_status": AccessibilityStatus.ACCESSIBLE,
                "commissioning_agency": "OSDMA",
            },
        ]

        summary = sync_ncrmp_shelters(session=mock_session, shelter_records=test_records)

        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.fetched, 2)
        self.assertEqual(summary.created, 2)
        self.assertEqual(summary.failed, 0)

        # Verify added objects
        added_objs = [call[0][0] for call in mock_session.add.call_args_list]
        locations = [o for o in added_objs if isinstance(o, SafeLocation)]
        refs = [o for o in added_objs if isinstance(o, SafeLocationSourceReference)]

        self.assertEqual(len(locations), 2)
        self.assertEqual(len(refs), 2)

        for loc in locations:
            self.assertEqual(loc.designation_status, DesignationStatus.OFFICIAL)
            self.assertEqual(loc.verification_status, VerificationStatus.VERIFIED)
            self.assertIn(loc.capacity, (1200, 1500))

        for ref in refs:
            self.assertEqual(ref.properties["verification_detail"], "OFFICIALLY_VERIFIED")
            self.assertIn("commissioning_agency", ref.properties)

    @patch("app.services.safe_location_osm.sync_data_sources")
    def test_osm_adapter_ingests_unverified_candidates_without_guessing_capacity(self, mock_sync_ds: MagicMock) -> None:
        """Verifies OSM adapter ingests candidate features as UNVERIFIED without fabricating capacity or condition."""
        mock_ds = MagicMock(spec=DataSource)
        mock_ds.id = uuid.uuid4()
        mock_ds.code = "OSM_OVERPASS"
        mock_ds.base_url = "https://overpass-api.de"

        mock_session = MagicMock()
        # 1: DataSource query -> mock_ds; next: ref and nearby checks return None
        mock_session.scalar.side_effect = [mock_ds, None, None, None, None]

        test_elements = [
            {
                "type": "node",
                "id": 9991,
                "lat": 19.8142,
                "lon": 85.8315,
                "tags": {
                    "name": "Bada Danda Open Assembly Ground",
                    "leisure": "common",
                },
            },
            {
                "type": "node",
                "id": 9992,
                "lat": 19.8050,
                "lon": 85.8220,
                "tags": {
                    "name": "Puri Headquarters Hospital",
                    "amenity": "hospital",
                    "wheelchair": "yes",
                },
            },
        ]

        summary = sync_osm_locations(session=mock_session, elements_data=test_elements)

        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.fetched, 2)
        self.assertEqual(summary.created, 2)

        added_objs = [call[0][0] for call in mock_session.add.call_args_list]
        locations = [o for o in added_objs if isinstance(o, SafeLocation)]
        refs = [o for o in added_objs if isinstance(o, SafeLocationSourceReference)]

        self.assertEqual(len(locations), 2)
        for loc in locations:
            # Strictly UNVERIFIED and POTENTIAL
            self.assertEqual(loc.designation_status, DesignationStatus.POTENTIAL)
            self.assertEqual(loc.verification_status, VerificationStatus.UNVERIFIED)
            # Never fabricate capacity or structural condition
            self.assertIsNone(loc.capacity)
            self.assertIsNone(loc.structural_condition)

        for ref in refs:
            self.assertEqual(ref.properties["verification_detail"], "SOURCE_LISTED_UNVERIFIED")
            self.assertIn("has not received official disaster shelter designation", ref.properties["verification_evidence"])

    @patch("app.services.safe_location_osm.sync_data_sources")
    def test_osm_adapter_preserves_verified_shelter_during_duplicate_linking(self, mock_sync_ds: MagicMock) -> None:
        """Verifies that an OSM feature near an official shelter links to it without downgrading its official status."""
        mock_ds = MagicMock(spec=DataSource)
        mock_ds.id = uuid.uuid4()
        mock_ds.code = "OSM_OVERPASS"

        # Existing officially verified shelter
        existing_shelter = SafeLocation(
            id=uuid.uuid4(),
            name="Official Cyclone Shelter",
            category="OFFICIAL_CYCLONE_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            capacity=1500,
            structural_condition=StructuralCondition.GOOD,
        )

        mock_session = MagicMock()
        # 1: DataSource query -> mock_ds
        # 2: ref_stmt -> None (not yet seen from OSM)
        # 3: nearby_stmt -> existing_shelter (located within 30m)
        mock_session.scalar.side_effect = [mock_ds, None, existing_shelter]

        test_elements = [
            {
                "type": "node",
                "id": 8881,
                "lat": 20.4285,
                "lon": 86.5812,
                "tags": {
                    "name": "Cyclone Shelter Building",
                    "amenity": "community_centre",
                },
            }
        ]

        summary = sync_osm_locations(session=mock_session, elements_data=test_elements)

        self.assertEqual(summary.updated, 1)  # Linked as updated reference
        self.assertEqual(summary.created, 0)

        # Official shelter MUST retain its official verified status!
        self.assertEqual(existing_shelter.designation_status, DesignationStatus.OFFICIAL)
        self.assertEqual(existing_shelter.verification_status, VerificationStatus.VERIFIED)
        self.assertEqual(existing_shelter.capacity, 1500)

    def test_get_nearby_safe_locations_partitions_counts_and_missing_info(self) -> None:
        """Verifies nearby locations retrieval partitions official vs candidate counts and lists missing info."""
        now = datetime.now(timezone.utc)

        # Location 1: Officially verified cyclone shelter with capacity
        loc1 = SafeLocation(
            id=uuid.uuid4(),
            name="Mahakalapara Multi-Purpose Cyclone Shelter",
            category="OFFICIAL_CYCLONE_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            location=None,
            boundary=None,
            capacity=1500,
            structural_condition=StructuralCondition.GOOD,
            accessibility_status=AccessibilityStatus.ACCESSIBLE,
            created_at=now,
            updated_at=now,
        )
        loc1.source_references = []

        # Location 2: Unverified open ground candidate (no capacity, no structural report)
        loc2 = SafeLocation(
            id=uuid.uuid4(),
            name="Central Park Assembly Ground",
            category="PARK",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            location=None,
            boundary=None,
            capacity=None,
            structural_condition=None,
            accessibility_status=None,
            created_at=now,
            updated_at=now,
        )
        loc2.source_references = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [
            (loc1, 1500.0),  # 1.5 km away
            (loc2, 3200.0),  # 3.2 km away
        ]

        response = get_nearby_safe_locations(
            session=mock_session,
            latitude=20.4,
            longitude=86.5,
            radius_km=25.0,
        )

        self.assertEqual(response.total_found, 2)
        self.assertEqual(response.official_shelters_count, 1)
        self.assertEqual(response.candidate_locations_count, 1)
        self.assertEqual(response.open_assembly_areas_count, 1)

        # Distances
        self.assertEqual(response.locations[0].distance_km, 1.5)
        self.assertEqual(response.locations[1].distance_km, 3.2)

        # Missing information on unverified candidate
        missing_cand = response.locations[1].missing_information
        self.assertTrue(any("capacity is unrecorded" in m for m in missing_cand))
        self.assertTrue(any("Structural engineering condition" in m for m in missing_cand))
        self.assertTrue(any("unverified candidate facility" in m for m in missing_cand))

        # Data integrity notice
        self.assertIn("geographical features, not proof of disaster safety", response.data_integrity_notice)

    def test_get_safe_location_by_id_returns_verification_evidence(self) -> None:
        """Verifies individual location details endpoint returns full verification evidence and limitations."""
        now = datetime.now(timezone.utc)
        loc_id = uuid.uuid4()
        ds_id = uuid.uuid4()

        mock_ds = MagicMock()
        mock_ds.id = ds_id
        mock_ds.code = "NCRMP_SHELTERS"
        mock_ds.name = "National Cyclone Risk Mitigation Project"

        mock_ref = MagicMock()
        mock_ref.data_source = mock_ds
        mock_ref.data_source_id = ds_id
        mock_ref.external_feature_id = "NCRMP_OD_KND_001"
        mock_ref.source_url = "https://ncrmp.gov.in"
        mock_ref.last_seen_at = now
        mock_ref.properties = {
            "verification_detail": "OFFICIALLY_VERIFIED",
            "commissioning_agency": "OSDMA",
        }

        loc = SafeLocation(
            id=loc_id,
            name="Official Cyclone Shelter",
            category="OFFICIAL_CYCLONE_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            location=None,
            boundary=None,
            capacity=1500,
            structural_condition=StructuralCondition.GOOD,
            accessibility_status=AccessibilityStatus.ACCESSIBLE,
            created_at=now,
            updated_at=now,
        )
        loc.source_references = [mock_ref]

        mock_session = MagicMock()
        mock_session.scalar.return_value = loc

        res = get_safe_location_by_id(session=mock_session, location_id=loc_id)
        self.assertIsNotNone(res)
        self.assertEqual(res.location.id, loc_id)
        self.assertEqual(res.location.verification_detail, "OFFICIALLY_VERIFIED")
        self.assertEqual(res.verification_evidence["total_source_references"], 1)
        self.assertIn("disaster-specific suitability evaluation is required", res.disaster_suitability_disclaimer.lower())

    def test_sync_all_safe_location_sources_orchestrator(self) -> None:
        """Verifies safe location sync orchestrator runs both official and candidate adapters idempotently."""
        mock_session = MagicMock()

        with patch("app.services.safe_location_ingestion.sync_ncrmp_shelters") as mock_ncrmp, \
             patch("app.services.safe_location_ingestion.sync_osm_locations") as mock_osm:

            mock_ncrmp.return_value = SourceSyncSummary(
                status="ACTIVE", message="NCRMP synced", created=5, updated=0, skipped=0, failed=0
            )
            mock_osm.return_value = SourceSyncSummary(
                status="ACTIVE", message="OSM synced", created=8, updated=1, skipped=0, failed=0
            )

            sync_resp = sync_all_safe_location_sources(session=mock_session)

            self.assertTrue(sync_resp.success)
            self.assertEqual(sync_resp.total_created, 13)
            self.assertEqual(sync_resp.total_updated, 1)
            self.assertEqual(len(sync_resp.sources), 2)
            self.assertIn("13 created", sync_resp.message)


if __name__ == "__main__":
    unittest.main()
