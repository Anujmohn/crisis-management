from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.api.v1.flood_assessment import get_flood_assessment
from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.flood_assessment import (
    FloodAssessmentResponse,
    FloodAssessmentStatus,
    GeographicalEnvironment,
    RiverLevelStatus,
)
from app.services.flood_assessment import (
    assess_flood_hazard,
    classify_geographical_environment,
    evaluate_rainfall_category,
    evaluate_river_level_status,
)


class FloodAssessmentTests(unittest.TestCase):
    def test_flood_assessment_route_registered(self) -> None:
        """Verifies GET /api/v1/flood/assessment is registered in OpenAPI schema with correct parameter constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/flood/assessment", paths)
        self.assertIn("get", paths["/api/v1/flood/assessment"])

        # Check parameter definitions and constraints
        params = {p["name"]: p for p in paths["/api/v1/flood/assessment"]["get"]["parameters"]}
        self.assertIn("latitude", params)
        self.assertIn("longitude", params)
        self.assertIn("search_radius_km", params)
        self.assertIn("lookback_days", params)

        # Latitude bounds
        lat_schema = params["latitude"]["schema"]
        self.assertEqual(lat_schema.get("minimum"), -90.0)
        self.assertEqual(lat_schema.get("maximum"), 90.0)
        self.assertTrue(params["latitude"]["required"])

        # Longitude bounds
        lon_schema = params["longitude"]["schema"]
        self.assertEqual(lon_schema.get("minimum"), -180.0)
        self.assertEqual(lon_schema.get("maximum"), 180.0)
        self.assertTrue(params["longitude"]["required"])

        # Search radius bounds & default
        radius_schema = params["search_radius_km"]["schema"]
        self.assertEqual(radius_schema.get("maximum"), 1000.0)
        self.assertEqual(radius_schema.get("default"), 50.0)

        # Lookback days bounds & default
        lookback_schema = params["lookback_days"]["schema"]
        self.assertEqual(lookback_schema.get("minimum"), 1)
        self.assertEqual(lookback_schema.get("maximum"), 365)
        self.assertEqual(lookback_schema.get("default"), 7)

    def test_classify_geographical_environment(self) -> None:
        """Verifies modular geographical environment classification (flat vs hilly)."""
        # 1. Slope-based classification
        env_steep, notes_steep = classify_geographical_environment(
            latitude=20.0,
            longitude=75.0,
            elevation_m=800.0,
            slope_degrees=18.0,
        )
        self.assertEqual(env_steep, GeographicalEnvironment.HILLY_OR_MOUNTAINOUS)
        self.assertIn("HILLY_OR_MOUNTAINOUS", notes_steep[0])

        env_flat, notes_flat = classify_geographical_environment(
            latitude=20.0,
            longitude=75.0,
            elevation_m=100.0,
            slope_degrees=2.0,
        )
        self.assertEqual(env_flat, GeographicalEnvironment.PLAIN_OR_FLAT)
        self.assertIn("PLAIN_OR_FLAT", notes_flat[0])

        # 2. Regional physiographic heuristic: Himalayas
        env_himalayas, notes_him = classify_geographical_environment(latitude=30.5, longitude=79.0)
        self.assertEqual(env_himalayas, GeographicalEnvironment.HILLY_OR_MOUNTAINOUS)

        # 3. Regional physiographic heuristic: Indo-Gangetic Plain (Patna)
        env_plains, notes_pl = classify_geographical_environment(latitude=25.6, longitude=85.1)
        self.assertEqual(env_plains, GeographicalEnvironment.PLAIN_OR_FLAT)

    def test_river_level_status_evaluation(self) -> None:
        """Verifies comparison of river levels against warning, danger, and HFL thresholds."""
        # Normal (below warning)
        res_normal = evaluate_river_level_status(
            water_level=20.0,
            warning_level=24.0,
            danger_level=25.0,
            hfl=26.5,
        )
        self.assertEqual(res_normal, RiverLevelStatus.BELOW_WARNING)

        # Above warning
        res_warn = evaluate_river_level_status(
            water_level=24.5,
            warning_level=24.0,
            danger_level=25.0,
            hfl=26.5,
        )
        self.assertEqual(res_warn, RiverLevelStatus.ABOVE_WARNING)

        # Above danger
        res_danger = evaluate_river_level_status(
            water_level=25.2,
            warning_level=24.0,
            danger_level=25.0,
            hfl=26.5,
        )
        self.assertEqual(res_danger, RiverLevelStatus.ABOVE_DANGER)

        # Above HFL
        res_hfl = evaluate_river_level_status(
            water_level=27.0,
            warning_level=24.0,
            danger_level=25.0,
            hfl=26.5,
        )
        self.assertEqual(res_hfl, RiverLevelStatus.ABOVE_HFL)

        # Unavailable water level (None != 0.0)
        res_none = evaluate_river_level_status(
            water_level=None,
            warning_level=24.0,
            danger_level=25.0,
            hfl=26.5,
        )
        self.assertEqual(res_none, RiverLevelStatus.UNKNOWN)

    def test_rainfall_category_evaluation(self) -> None:
        """Verifies mapping of rainfall intensities to IMD categories."""
        self.assertEqual(evaluate_rainfall_category(None), "NONE_REPORTED")
        self.assertEqual(evaluate_rainfall_category(0.0), "NONE_REPORTED")
        self.assertEqual(evaluate_rainfall_category(3.0), "LIGHT")
        self.assertEqual(evaluate_rainfall_category(10.0), "MODERATE")
        self.assertEqual(evaluate_rainfall_category(25.0), "HEAVY")
        self.assertEqual(evaluate_rainfall_category(120.0), "VERY_HEAVY")
        self.assertEqual(evaluate_rainfall_category(210.0), "EXTREMELY_HEAVY")

    def test_assess_flood_hazard_no_evidence(self) -> None:
        """Verifies NO_RELEVANT_FLOOD_EVIDENCE status and mandatory non-safety disclaimer."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(response.assessment_status, FloodAssessmentStatus.NO_RELEVANT_FLOOD_EVIDENCE)
        self.assertEqual(len(response.observed_flooding), 0)
        self.assertEqual(len(response.official_warnings), 0)
        self.assertEqual(len(response.river_observations), 0)
        self.assertEqual(len(response.rainfall_information), 0)

        # Mandatory disclaimer presence
        self.assertIn("No relevant flood event, official warning, or abnormal river gauge was found", response.summary_message)
        self.assertIn("This does not establish that the location is safe.", response.summary_message)
        self.assertTrue(len(response.scientific_limitations) > 0)
        self.assertTrue(len(response.missing_information) > 0)

    def test_assess_flood_hazard_observed_flooding(self) -> None:
        """Verifies OBSERVED_FLOODING status when actual reported inundation is detected."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.FLOOD
        mock_event.title = "Inundation Observed in Low-Lying Sector"
        mock_event.description = "Severe waterlogging and flood observed"
        mock_event.severity = SeverityLevel.HIGH
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [77.25, 28.65]}
        mock_event.affected_area = None
        mock_event.started_at = now - timedelta(hours=3)
        mock_event.expires_at = None
        mock_event.first_seen_at = now - timedelta(hours=3)
        mock_event.last_seen_at = now - timedelta(minutes=30)
        mock_event.source_reports = []

        mock_session = MagicMock()
        # Events returned
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 5000.0, None)],  # Event query
            [],                            # Observations query
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(response.assessment_status, FloodAssessmentStatus.OBSERVED_FLOODING)
        self.assertEqual(len(response.observed_flooding), 1)
        self.assertIn("Active flood inundation observed", response.summary_message)

    def test_assess_flood_hazard_official_warning_intersecting(self) -> None:
        """Verifies OFFICIAL_FLOOD_WARNING when user intersects warning polygon or river is above danger."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.FLOOD
        mock_event.title = "Red Warning: District Flash Flood Alert"
        mock_event.description = "Extremely heavy rainfall and flash flood alert from IMD"
        mock_event.severity = SeverityLevel.EXTREME
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = None
        # User is at (28.6, 77.2) which is inside this polygon
        mock_event.affected_area = {
            "type": "Polygon",
            "coordinates": [
                [
                    [77.0, 28.4],
                    [77.5, 28.4],
                    [77.5, 28.8],
                    [77.0, 28.8],
                    [77.0, 28.4],
                ]
            ],
        }
        mock_event.started_at = now - timedelta(hours=1)
        mock_event.expires_at = now + timedelta(hours=4)
        mock_event.first_seen_at = now - timedelta(hours=1)
        mock_event.last_seen_at = now - timedelta(minutes=10)
        mock_event.source_reports = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 0.0, 0.0)],  # Intersecting event
            [],                        # No observations
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            search_radius_km=50.0,
            lookback_days=3,
        )

        self.assertEqual(response.assessment_status, FloodAssessmentStatus.OFFICIAL_FLOOD_WARNING)
        self.assertEqual(len(response.official_warnings), 1)
        self.assertTrue(response.official_warnings[0].intersects_user_location)
        self.assertIn("intersects an active official flood/heavy-rainfall warning zone", response.summary_message)

    def test_assess_flood_hazard_elevated_susceptibility(self) -> None:
        """Verifies ELEVATED_FLOOD_SUSCEPTIBILITY when river is above warning level or heavy rainfall reported."""
        now = datetime.now(timezone.utc)

        # Mock CWC river gauge above warning level
        mock_obs = MagicMock()
        mock_obs.id = uuid.uuid4()
        mock_obs.observation_type = "RIVER_WATER_LEVEL"
        mock_obs.value_numeric = Decimal("24.5")
        mock_obs.value_text = "24.5 m"
        mock_obs.geometry = {"type": "Point", "coordinates": [77.3, 28.7]}
        mock_obs.observed_at = now - timedelta(hours=2)
        mock_obs.ingested_at = now - timedelta(minutes=20)
        mock_obs.data_source = MagicMock()
        mock_obs.data_source.code = "CWC_FLOOD"
        mock_obs.data_source.name = "Central Water Commission"
        mock_obs.data_source.source_type = "HYDROLOGY_TELEMETRY"
        mock_obs.data_source.base_url = "https://ffs.india-water.gov.in"
        mock_obs.properties = {
            "station_code": "CWC_DELHI_01",
            "station_name": "Old Railway Bridge",
            "river_name": "Yamuna",
            "basin": "Ganga Basin",
            "warning_level_m": 24.0,
            "danger_level_m": 25.0,
            "hfl_m": 26.5,
        }

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [],                      # No events
            [(mock_obs, 12000.0)],   # River gauge 12 km away
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(response.assessment_status, FloodAssessmentStatus.ELEVATED_FLOOD_SUSCEPTIBILITY)
        self.assertEqual(len(response.river_observations), 1)
        self.assertEqual(response.river_observations[0].status_relative_to_danger, RiverLevelStatus.ABOVE_WARNING)
        self.assertIn("Elevated flood susceptibility detected", response.summary_message)

    def test_assess_flood_hazard_conflicting_sources(self) -> None:
        """Verifies source divergence notes when heavy rainfall warning coincides with normal river gauges."""
        now = datetime.now(timezone.utc)

        # Rainfall obs (heavy rain)
        rain_obs = MagicMock()
        rain_obs.id = uuid.uuid4()
        rain_obs.observation_type = "RAINFALL_INTENSITY"
        rain_obs.value_numeric = Decimal("35.0")
        rain_obs.value_text = "Heavy Rain: > 15 mm/hr"
        rain_obs.geometry = {"type": "Point", "coordinates": [77.2, 28.6]}
        rain_obs.observed_at = now - timedelta(hours=1)
        rain_obs.ingested_at = now
        rain_obs.data_source = MagicMock()
        rain_obs.data_source.code = "IMD_WEATHER_API"
        rain_obs.data_source.name = "India Meteorological Department"
        rain_obs.data_source.source_type = "OGC_WFS"
        rain_obs.data_source.base_url = "https://mausam.imd.gov.in"
        rain_obs.properties = {"district": "New Delhi", "color_code": 3}

        # River gauge (normal, well below warning)
        river_obs = MagicMock()
        river_obs.id = uuid.uuid4()
        river_obs.observation_type = "RIVER_WATER_LEVEL"
        river_obs.value_numeric = Decimal("18.0")
        river_obs.value_text = "18.0 m"
        river_obs.geometry = {"type": "Point", "coordinates": [77.3, 28.7]}
        river_obs.observed_at = now - timedelta(hours=1)
        river_obs.ingested_at = now
        river_obs.data_source = MagicMock()
        river_obs.data_source.code = "CWC_FLOOD"
        river_obs.data_source.name = "Central Water Commission"
        river_obs.data_source.source_type = "HYDROLOGY_TELEMETRY"
        river_obs.data_source.base_url = "https://ffs.india-water.gov.in"
        river_obs.properties = {
            "station_code": "CWC_01",
            "warning_level_m": 24.0,
            "danger_level_m": 25.0,
        }

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [],                                 # No events
            [(rain_obs, 5000.0), (river_obs, 12000.0)],  # Both observations
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(len(response.rainfall_information), 1)
        self.assertEqual(len(response.river_observations), 1)
        # Check source divergence note
        divergence_noted = any("Source Divergence" in factor for factor in response.contributing_factors)
        self.assertTrue(divergence_noted)

    def test_assess_flood_hazard_stale_data(self) -> None:
        """Verifies DATA_STALE status when records exceed validity and freshness thresholds."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=5)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.FLOOD
        mock_event.title = "Expired Historical Warning"
        mock_event.description = "Past flood warning"
        mock_event.severity = SeverityLevel.LOW
        mock_event.status = CrisisStatus.EXPIRED
        mock_event.location = {"type": "Point", "coordinates": [77.3, 28.6]}
        mock_event.affected_area = None
        mock_event.started_at = old_time
        mock_event.expires_at = old_time + timedelta(hours=12)
        mock_event.first_seen_at = old_time
        mock_event.last_seen_at = old_time
        mock_event.source_reports = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 10000.0, None)],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_flood_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(response.assessment_status, FloodAssessmentStatus.DATA_STALE)

    def test_api_endpoint_invokes_service(self) -> None:
        """Verifies get_flood_assessment endpoint function delegates to service."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        res = get_flood_assessment(
            latitude=28.6139,
            longitude=77.2090,
            search_radius_km=50.0,
            lookback_days=7,
            session=mock_session,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.assessment_status, FloodAssessmentStatus.NO_RELEVANT_FLOOD_EVIDENCE)
        self.assertEqual(res.user_location.latitude, 28.6139)
        self.assertEqual(res.user_location.longitude, 77.2090)


if __name__ == "__main__":
    unittest.main()
