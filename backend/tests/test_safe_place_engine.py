from __future__ import annotations

import unittest
import uuid
from unittest.mock import MagicMock

from app.main import app
from app.models.core import CrisisEvent
from app.models.enums import (
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    VerificationStatus,
)
from app.models.recommendations import SafeLocation
from app.schemas.recommendation import SuitabilityClassification
from app.schemas.safe_place import FindSafePlacesResponse, SafePlaceItem
from app.services.safe_place_engine import (
    compute_cyclone_safety_fields,
    compute_earthquake_safety_fields,
    compute_flood_safety_fields,
    find_safe_places_near_user,
    is_real_location_suitable_for_crisis,
)


class SafePlaceEngineTests(unittest.TestCase):
    """
    Verification tests for the Separate Safe Place Discovery Engine:
    - 0.1 to 5.0 km radius constraint (under in the 1 to 5 km).
    - Starts search at 100 meters (0.1 km).
    - Computes 1.a Earthquake fields (distance, area, open places, hilly stone crushing).
    - Computes 3.b Flood fields (distance, elevation, safety factor, flyovers/govt buildings, flow prediction, transect MSL).
    - Computes 3.c Sea Typhoon fields (distance, beach vs inland effect, stilt MPCS safety factor).
    """

    def test_earthquake_fields_calculation(self):
        # Open garden in flat terrain
        garden = SafeLocation(
            id=uuid.uuid4(),
            name="Community Garden & Public Lawns",
            category="GARDEN",
            capacity=2000,
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        eq_fields_flat = compute_earthquake_safety_fields(
            loc=garden, lat=25.5941, lon=85.1376, distance_km=0.1
        )
        self.assertEqual(eq_fields_flat.distance_from_user_km, 0.1)
        self.assertEqual(eq_fields_flat.distance_from_user_m, 100.0)
        self.assertEqual(eq_fields_flat.safe_area_m2, 7000.0)  # 2000 * 3.5
        self.assertTrue(eq_fields_flat.is_open_place)
        self.assertEqual(eq_fields_flat.stone_crushing_risk, "ZERO")
        self.assertIn("clear skyward clearance", eq_fields_flat.building_collapse_protection.lower())

        # In mountainous terrain (Uttarakhand)
        eq_fields_hilly = compute_earthquake_safety_fields(
            loc=garden, lat=30.3165, lon=78.0322, distance_km=0.15
        )
        self.assertEqual(eq_fields_hilly.stone_crushing_risk, "ELEVATED_SLOPE_HAZARD")
        self.assertIn("stone crushing", eq_fields_hilly.hilly_area_safety.lower())

    def test_flood_fields_calculation(self):
        flyover = SafeLocation(
            id=uuid.uuid4(),
            name="Elevated Flyover Platform & Viaduct",
            category="FLYOVER",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        flood_ev = CrisisEvent(
            id=uuid.uuid4(),
            crisis_type=CrisisType.FLOOD,
            status=CrisisStatus.ACTIVE,
            location="SRID=4326;POINT(85.1000 25.6200)",
        )
        fl_fields = compute_flood_safety_fields(
            loc=flyover,
            lat=25.6118,
            lon=85.1185,
            distance_km=0.26,
            active_flood_events=[flood_ev],
            user_lat=25.6100,
            user_lon=85.1200,
        )
        self.assertEqual(fl_fields.distance_from_user_km, 0.26)
        self.assertGreater(fl_fields.elevation_m, 10.0)
        self.assertTrue(fl_fields.is_safe_building)
        self.assertEqual(fl_fields.safe_building_type, "ELEVATED_FLYOVER_REFUGE")
        self.assertIn("HIGH_SAFETY", fl_fields.safety_factor)
        self.assertGreater(len(fl_fields.middle_transect_heights_msl), 0)
        self.assertTrue(fl_fields.is_elevated_above_transect)

    def test_cyclone_fields_calculation(self):
        # Open beach in Puri
        beach = SafeLocation(
            id=uuid.uuid4(),
            name="Puri Beach Open Frontage",
            category="OPEN_GROUND",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        cyc_beach = compute_cyclone_safety_fields(
            loc=beach, lat=19.8000, lon=85.8300, distance_km=0.1
        )
        self.assertIn("DANGEROUS / PROHIBITED", cyc_beach.safety_factor_of_area)
        self.assertLessEqual(cyc_beach.distance_to_coastline_km, 10.0)

        # Multi-Purpose Cyclone Shelter
        mpcs = SafeLocation(
            id=uuid.uuid4(),
            name="NCRMP Cyclone Shelter",
            category="OFFICIAL_CYCLONE_SHELTER",
            capacity=1500,
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
        )
        cyc_mpcs = compute_cyclone_safety_fields(
            loc=mpcs, lat=19.8200, lon=85.8200, distance_km=1.2
        )
        self.assertIn("HIGH_SAFETY", cyc_mpcs.safety_factor_of_area)
        self.assertIn("Stilt", cyc_mpcs.cyclone_shelter_engineering)

    def test_find_safe_places_api_route_registered(self):
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/safe-locations/find-safe-places", paths)

    def test_crisis_suitability_filter_isolation(self):
        # 1. Reject synthetic / hypothetical names
        fake_flyover = SafeLocation(
            id=uuid.uuid4(),
            name="Elevated Flyover Platform & Refuge Zone (25.594, 85.138)",
            category="FLYOVER",
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                fake_flyover, CrisisType.FLOOD, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE, 85.0
            )
        )

        fake_ground = SafeLocation(
            id=uuid.uuid4(),
            name="Public Garden & Open Lawn Assembly Zone (25.594, 85.138)",
            category="OPEN_GROUND",
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                fake_ground, CrisisType.EARTHQUAKE, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE, 90.0
            )
        )

        # 2. Real open park/garden: valid for Earthquake, NOT for Flood or Cyclone
        real_park = SafeLocation(
            id=uuid.uuid4(),
            name="Lodhi Gardens Public Grounds",
            category="OPEN_GROUND",
        )
        self.assertTrue(
            is_real_location_suitable_for_crisis(
                real_park, CrisisType.EARTHQUAKE, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE, 90.0
            )
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                real_park, CrisisType.FLOOD, SuitabilityClassification.CONDITIONALLY_SUITABLE, 50.0
            )
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                real_park, CrisisType.CYCLONE, SuitabilityClassification.CONDITIONALLY_SUITABLE, 50.0
            )
        )

        # 3. Real elevated flyover: valid for Flood, NOT for Earthquake or Cyclone
        real_flyover = SafeLocation(
            id=uuid.uuid4(),
            name="Barapullah Elevated Corridor, New Delhi",
            category="FLYOVER",
        )
        self.assertTrue(
            is_real_location_suitable_for_crisis(
                real_flyover, CrisisType.FLOOD, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE, 90.0
            )
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                real_flyover, CrisisType.EARTHQUAKE, SuitabilityClassification.CONDITIONALLY_SUITABLE, 60.0
            )
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                real_flyover, CrisisType.CYCLONE, SuitabilityClassification.CONDITIONALLY_SUITABLE, 60.0
            )
        )

        # 4. Real multi-purpose cyclone shelter: valid for Cyclone, NOT for Earthquake (enclosed structure)
        real_cyclone_shelter = SafeLocation(
            id=uuid.uuid4(),
            name="Puri Multipurpose Cyclone Shelter MPCS-04",
            category="OFFICIAL_CYCLONE_SHELTER",
        )
        self.assertTrue(
            is_real_location_suitable_for_crisis(
                real_cyclone_shelter, CrisisType.CYCLONE, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE, 95.0
            )
        )
        self.assertFalse(
            is_real_location_suitable_for_crisis(
                real_cyclone_shelter, CrisisType.EARTHQUAKE, SuitabilityClassification.CONDITIONALLY_SUITABLE, 60.0
            )
        )


if __name__ == "__main__":
    unittest.main()
