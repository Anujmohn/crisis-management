from __future__ import annotations

import unittest
import uuid
from unittest.mock import MagicMock

from app.api.v1.safe_locations import (
    get_elevation_between_crisis_and_user,
    get_flood_elevation_analysis,
)
from app.main import app
from app.models.enums import CrisisType, DesignationStatus, VerificationStatus
from app.models.recommendations import SafeLocation
from app.services.city_elevation_data import (
    INDIAN_CITIES_ELEVATION_MSL,
    calculate_elevation_difference,
    find_nearest_city_elevation,
    get_city_height_from_sea_level,
)
from app.services.elevated_place_engine import (
    find_elevated_places_near_user,
    is_elevated_place_candidate,
)
from app.services.elevation_transect_engine import (
    compute_elevation_between_crisis_and_user,
)


class ElevationEnginesTests(unittest.TestCase):
    """
    Verification tests for the Elevation and Flood Engines:
    1. Separate engine to find elevation between user and crisis starting point (with middle points).
    2. Separate engine for finding elevated places (open field, flyover) with more height than user location starting at 100m.
    3. Combined data showing flood flow and safe locations.
    4. City height from sea level dataset and difference calculation (whichever has greater value has high elevation).
    """

    def test_city_elevation_dataset_coverage_and_accuracy(self):
        # Patna: 53.0 m MSL
        patna_h = get_city_height_from_sea_level(25.5941, 85.1376)
        self.assertEqual(patna_h, 53.0)

        # New Delhi: 216.0 m MSL
        delhi_h = get_city_height_from_sea_level(28.6139, 77.2090)
        self.assertEqual(delhi_h, 216.0)

        # Dehradun: 640.0 m MSL
        doon_h = get_city_height_from_sea_level(30.3165, 78.0322)
        self.assertEqual(doon_h, 640.0)

        # Nearest city finder
        city, elev, dist, state, basin = find_nearest_city_elevation(25.6000, 85.1400)
        self.assertEqual(city, "Patna")
        self.assertEqual(elev, 53.0)
        self.assertLess(dist, 5.0)

    def test_elevation_difference_greater_value_rule(self):
        # Varanasi (81m) vs Patna (53m): Varanasi has greater value and high elevation
        diff_info = calculate_elevation_difference("Varanasi", 81.0, "Patna", 53.0)
        self.assertEqual(diff_info["difference_m"], 28.0)
        self.assertEqual(diff_info["higher_location"], "Varanasi")
        self.assertEqual(diff_info["lower_location"], "Patna")
        self.assertTrue(diff_info["is_loc1_higher"])
        self.assertIn("Varanasi", diff_info["summary"])
        self.assertIn("HIGHER by 28.0 meters", diff_info["summary"])

        # Patna vs Varanasi: Varanasi still has high elevation
        diff_info2 = calculate_elevation_difference("Patna", 53.0, "Varanasi", 81.0)
        self.assertEqual(diff_info2["difference_m"], 28.0)
        self.assertEqual(diff_info2["higher_location"], "Varanasi")
        self.assertFalse(diff_info2["is_loc1_higher"])

    def test_elevation_transect_between_crisis_and_user(self):
        # Flood starts near Buxar/Varanasi (upstream Ganges ~80m MSL), user in Patna (~53m MSL)
        crisis_lat, crisis_lon = 25.5600, 83.9800  # Upstream
        user_lat, user_lon = 25.5941, 85.1376     # Patna downstream

        res = compute_elevation_between_crisis_and_user(
            crisis_lat=crisis_lat,
            crisis_lon=crisis_lon,
            user_lat=user_lat,
            user_lon=user_lon,
            num_middle_samples=5,
        )

        # Verify crisis start point and user point
        self.assertGreater(res.crisis_start_height_msl, res.user_height_msl)
        self.assertTrue(res.is_crisis_higher)
        self.assertFalse(res.is_user_higher)
        self.assertEqual(res.flow_direction, "DOWNHILL_TOWARDS_USER")

        # Verify middle transect points
        self.assertEqual(len(res.middle_transect_nodes), 5)
        self.assertEqual(len(res.middle_heights_msl), 5)
        for node in res.middle_transect_nodes:
            self.assertGreater(node.height_from_sea_level_m, 0.0)
            self.assertIsNotNone(node.nearest_city)

        # Verify flood flow GeoJSON
        self.assertEqual(res.flood_flow_path_geojson["type"], "FeatureCollection")
        self.assertEqual(len(res.flood_flow_path_geojson["features"]), 1)
        coords = res.flood_flow_path_geojson["features"][0]["geometry"]["coordinates"]
        self.assertEqual(len(coords), 7)  # crisis + 5 middle nodes + user

    def test_elevated_place_candidate_identification(self):
        # Real flyover
        flyover = SafeLocation(
            id=uuid.uuid4(),
            name="Bailey Road Elevated Flyover, Patna",
            category="FLYOVER",
        )
        is_cand, p_type = is_elevated_place_candidate(flyover)
        self.assertTrue(is_cand)
        self.assertEqual(p_type, "ELEVATED_FLYOVER")

        # Real high-elevation open field / stadium
        stadium = SafeLocation(
            id=uuid.uuid4(),
            name="Moin-ul-Haq Stadium Assembly Field",
            category="OPEN_GROUND",
        )
        is_cand2, p_type2 = is_elevated_place_candidate(stadium)
        self.assertTrue(is_cand2)
        self.assertEqual(p_type2, "HIGH_ELEVATION_OPEN_FIELD")

        # Synthetic/placeholder location must be rejected
        fake_loc = SafeLocation(
            id=uuid.uuid4(),
            name="Elevated Flyover Platform & Refuge Zone (25.594, 85.138)",
            category="FLYOVER",
        )
        is_cand3, _ = is_elevated_place_candidate(fake_loc)
        self.assertFalse(is_cand3)

    def test_api_routes_registered_in_openapi(self):
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/safe-locations/elevation-between-crisis-and-user", paths)
        self.assertIn("/api/v1/safe-locations/elevated-places", paths)
        self.assertIn("/api/v1/safe-locations/flood-elevation-analysis", paths)

    def test_direct_elevation_between_crisis_and_user_handler(self):
        transect = get_elevation_between_crisis_and_user(
            crisis_latitude=25.5600,
            crisis_longitude=83.9800,
            user_latitude=25.5941,
            user_longitude=85.1376,
            num_middle_samples=4,
        )
        self.assertEqual(transect.flow_direction, "DOWNHILL_TOWARDS_USER")
        self.assertEqual(len(transect.middle_transect_nodes), 4)
        self.assertGreater(transect.crisis_start_height_msl, transect.user_height_msl)
        self.assertIn("Crisis Starting Point", transect.higher_location_name)

    def test_direct_flood_elevation_analysis_handler(self):
        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_session.scalar.return_value = 5
        mock_session.execute.return_value.all.return_value = []
        mock_session.scalars.return_value.first.return_value = None

        analysis = get_flood_elevation_analysis(
            user_latitude=25.5941,
            user_longitude=85.1376,
            crisis_latitude=25.7000,
            crisis_longitude=85.0000,
            session=mock_session,
        )
        self.assertTrue(analysis.success)
        self.assertIsNotNone(analysis.elevation_transect)
        self.assertIsNotNone(analysis.flood_flow_path_geojson)
        self.assertIsNotNone(analysis.combined_safety_summary)


if __name__ == "__main__":
    unittest.main()
