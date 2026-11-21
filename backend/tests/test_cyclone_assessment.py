from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.cyclone_assessment import get_cyclone_assessment
from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.cyclone_assessment import (
    CycloneAssessmentResponse,
    CycloneAssessmentStatus,
)
from app.services.cyclone_assessment import (
    assess_cyclone_hazard,
    calculate_distance_to_indian_coastline_km,
    classify_cyclone_intensity,
    normalize_wind_speed_to_kmph,
)


class CycloneAssessmentTests(unittest.TestCase):
    def test_cyclone_assessment_route_registered(self) -> None:
        """Verifies GET /api/v1/cyclone/assessment is registered in OpenAPI schema with correct parameter constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/cyclone/assessment", paths)
        self.assertIn("get", paths["/api/v1/cyclone/assessment"])

        params = {p["name"]: p for p in paths["/api/v1/cyclone/assessment"]["get"]["parameters"]}
        self.assertIn("latitude", params)
        self.assertIn("longitude", params)
        self.assertIn("search_radius_km", params)
        self.assertIn("lookback_days", params)

        # Latitude constraints
        lat_schema = params["latitude"]["schema"]
        self.assertEqual(lat_schema.get("minimum"), -90.0)
        self.assertEqual(lat_schema.get("maximum"), 90.0)
        self.assertTrue(params["latitude"]["required"])

        # Longitude constraints
        lon_schema = params["longitude"]["schema"]
        self.assertEqual(lon_schema.get("minimum"), -180.0)
        self.assertEqual(lon_schema.get("maximum"), 180.0)
        self.assertTrue(params["longitude"]["required"])

        # Search radius constraints
        radius_schema = params["search_radius_km"]["schema"]
        self.assertEqual(radius_schema.get("maximum"), 2000.0)
        self.assertEqual(radius_schema.get("default"), 500.0)

        # Lookback days constraints
        lookback_schema = params["lookback_days"]["schema"]
        self.assertEqual(lookback_schema.get("minimum"), 1)
        self.assertEqual(lookback_schema.get("maximum"), 365)
        self.assertEqual(lookback_schema.get("default"), 7)

    def test_calculate_distance_to_indian_coastline(self) -> None:
        """Verifies accurate coastal proximity calculation and inland vs coastal classification."""
        # Coastal locations: Mumbai (~0 km), Puri (~0 km), Chennai (~0 km)
        mumbai_dist = calculate_distance_to_indian_coastline_km(18.95, 72.82)
        self.assertLessEqual(mumbai_dist, 25.0)

        puri_dist = calculate_distance_to_indian_coastline_km(19.80, 85.82)
        self.assertLessEqual(puri_dist, 25.0)

        chennai_dist = calculate_distance_to_indian_coastline_km(13.08, 80.27)
        self.assertLessEqual(chennai_dist, 25.0)

        # Inland locations: New Delhi (>900 km), Nagpur (>500 km), Bhopal (>400 km)
        delhi_dist = calculate_distance_to_indian_coastline_km(28.6139, 77.2090)
        self.assertGreater(delhi_dist, 800.0)

        nagpur_dist = calculate_distance_to_indian_coastline_km(21.1458, 79.0882)
        self.assertGreater(nagpur_dist, 400.0)

    def test_wind_speed_normalization_and_intensity_classification(self) -> None:
        """Verifies conversion of wind speeds to km/h and mapping to IMD cyclone intensity scale."""
        # Normalization
        self.assertIsNone(normalize_wind_speed_to_kmph(None))
        self.assertEqual(normalize_wind_speed_to_kmph(Decimal("45"), "knots"), 83.3)
        self.assertEqual(normalize_wind_speed_to_kmph(65, "kt"), 120.4)
        self.assertEqual(normalize_wind_speed_to_kmph(25, "m/s"), 90.0)
        self.assertEqual(normalize_wind_speed_to_kmph(50, "mph"), 80.5)
        self.assertEqual(normalize_wind_speed_to_kmph(100, "kmph"), 100.0)

        # IMD Classification
        self.assertEqual(classify_cyclone_intensity(None), "UNKNOWN")
        self.assertEqual(classify_cyclone_intensity(25.0), "LOW_PRESSURE_AREA")
        self.assertEqual(classify_cyclone_intensity(40.0), "DEPRESSION")
        self.assertEqual(classify_cyclone_intensity(55.0), "DEEP_DEPRESSION")
        self.assertEqual(classify_cyclone_intensity(75.0), "CYCLONIC_STORM")
        self.assertEqual(classify_cyclone_intensity(105.0), "SEVERE_CYCLONIC_STORM")
        self.assertEqual(classify_cyclone_intensity(135.0), "VERY_SEVERE_CYCLONIC_STORM")
        self.assertEqual(classify_cyclone_intensity(180.0), "EXTREMELY_SEVERE_CYCLONIC_STORM")
        self.assertEqual(classify_cyclone_intensity(230.0), "SUPER_CYCLONIC_STORM")

    def test_assess_cyclone_hazard_no_relevant_evidence(self) -> None:
        """Verifies NO_RELEVANT_HAZARD_EVIDENCE status and mandatory non-safety disclaimer when no events exist."""
        mock_session = MagicMock()
        # Event query returns empty, Standalone observation query returns empty
        mock_session.execute.return_value.all.side_effect = [
            [],  # Cyclone CrisisEvents
            [],  # Standalone HazardObservations
            [],  # Flood events
            [],  # Flood observations
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            search_radius_km=500.0,
            lookback_days=7,
        )

        self.assertEqual(response.primary_status, CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE)
        self.assertIn(CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE, response.assessment_statuses)
        self.assertEqual(len(response.relevant_cyclone_events), 0)
        self.assertEqual(len(response.official_warnings), 0)

        # Mandatory disclaimer verification
        self.assertIn(
            "No relevant cyclone event, official warning, or oceanic disturbance was found in the available dataset",
            response.summary_message,
        )
        self.assertIn("This does not establish that the location is safe.", response.summary_message)

        # Scientific limitations and missing information present
        self.assertTrue(len(response.scientific_limitations) > 0)
        self.assertTrue(len(response.missing_information) > 0)

    def test_assess_cyclone_hazard_coastal_active_cyclone(self) -> None:
        """Verifies multi-status evaluation for coastal location exposed to active cyclone, surge, and gale winds."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "IMD Severe Cyclonic Disturbance — Bay of Bengal"
        mock_event.description = "Very Severe Cyclonic Storm approaching Odisha coast"
        mock_event.severity = SeverityLevel.SEVERE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [86.2, 19.5]}
        mock_event.affected_area = None
        mock_event.started_at = now - timedelta(hours=4)
        mock_event.expires_at = now + timedelta(hours=24)
        mock_event.last_seen_at = now - timedelta(minutes=15)

        # Mock source report with forecast track and extracted observations
        mock_source = MagicMock()
        mock_source.code = "IMD_WEATHER_API"
        mock_source.name = "India Meteorological Department"
        mock_source.source_type = "API"
        mock_source.status = "ACTIVE"
        mock_source.last_successful_fetch = now

        mock_report = MagicMock()
        mock_report.data_source = mock_source
        mock_report.source_url = "https://example.com"
        mock_report.external_event_id = "IMD_CYCLONE_101"
        mock_report.issued_at = now - timedelta(hours=4)
        mock_report.fetched_at = now
        mock_report.valid_until = now + timedelta(hours=24)
        mock_report.raw_payload = {
            "cyclone_name": "DANA",
            "cyclone_id": 101,
            "cyclone_type": "very_severe_cyclone",
            "headline": "Cyclone Warning for Odisha and West Bengal coasts",
            "event": "Cyclone Alert",
            "forecast_track": [
                {"waypoint_index": 0, "latitude": 19.5, "longitude": 86.2, "position_type": "current"},
                {"waypoint_index": 1, "latitude": 20.2, "longitude": 86.8, "position_type": "forecast", "max_wind": 130},
                {"waypoint_index": 2, "latitude": 20.9, "longitude": 87.2, "position_type": "forecast", "max_wind": 140},
            ],
            "extracted_observations": {
                "sustained_wind_speed": "125.0",
                "gust_speed": "150.0",
                "wave_height": "4.5",
            },
        }

        mock_event.source_reports = [mock_report]

        # Add hazard observation for storm surge
        mock_surge_obs = MagicMock()
        mock_surge_obs.observation_type = "STORM_SURGE_HEIGHT"
        mock_surge_obs.value_numeric = Decimal("2.8")
        mock_surge_obs.unit = "m"
        mock_surge_obs.data_source = mock_source
        mock_event.hazard_observations = [mock_surge_obs]

        mock_session = MagicMock()
        # Side effect for queries:
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 45000.0, True)],  # Cyclone event query (45 km away, intersects)
            [],                             # Standalone observations query
            [],                             # Flood events query
            [],                             # Flood observations query
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=19.81,  # Puri, coastal Odisha
            longitude=85.83,
            search_radius_km=500.0,
            lookback_days=7,
        )

        # Verification of statuses
        self.assertIn(CycloneAssessmentStatus.OFFICIAL_CYCLONE_WARNING, response.assessment_statuses)
        self.assertIn(CycloneAssessmentStatus.WIND_HAZARD_CONCERN, response.assessment_statuses)
        self.assertIn(CycloneAssessmentStatus.STORM_SURGE_WARNING, response.assessment_statuses)
        self.assertIn(CycloneAssessmentStatus.COASTAL_INUNDATION_CONCERN, response.assessment_statuses)
        self.assertIn(CycloneAssessmentStatus.CYCLONE_POSITION_RELEVANT, response.assessment_statuses)

        # Wind hazard details
        self.assertTrue(response.wind_hazard.active_wind_hazard)
        self.assertEqual(response.wind_hazard.cyclone_intensity_category, "VERY_SEVERE_CYCLONIC_STORM")
        self.assertGreaterEqual(response.wind_hazard.max_sustained_wind_kmph, 120.0)

        # Storm surge & coastal inundation
        self.assertTrue(response.storm_surge.surge_warning_active)
        self.assertEqual(response.storm_surge.estimated_surge_height_m, 2.8)
        self.assertTrue(response.coastal_inundation.is_coastal_zone)
        self.assertTrue(response.coastal_inundation.coastal_inundation_concern)

        # Track waypoints preserved
        self.assertEqual(len(response.forecast_track_waypoints), 3)
        self.assertIsNotNone(response.latest_observed_position)
        self.assertEqual(response.latest_observed_position.position_type, "OBSERVED")

    def test_assess_cyclone_hazard_inland_location(self) -> None:
        """Verifies that inland locations are marked safe from direct storm surge while assessing wind/rain hazards."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "Depression moving inland"
        mock_event.description = "Weakening depression over central India"
        mock_event.severity = SeverityLevel.MODERATE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [79.2, 21.3]}
        mock_event.affected_area = None
        mock_event.started_at = now - timedelta(hours=2)
        mock_event.expires_at = now + timedelta(hours=12)
        mock_event.last_seen_at = now - timedelta(minutes=10)
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 35000.0, False)],  # Event 35 km away in central India
            [],
            [],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=21.1458,  # Nagpur (Inland central India)
            longitude=79.0882,
            search_radius_km=500.0,
            lookback_days=7,
        )

        # Coastal checks: must NOT be coastal zone
        self.assertFalse(response.coastal_inundation.is_coastal_zone)
        self.assertFalse(response.coastal_inundation.coastal_inundation_concern)
        self.assertFalse(response.storm_surge.surge_warning_active)
        self.assertIn("Inland location", response.storm_surge.notes[0])

    def test_forecast_track_waypoints_distinguished_from_observed_position(self) -> None:
        """Verifies that observed cyclone center is never overwritten by forecast track waypoints."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "IMD Tropical Cyclone Advisory"
        mock_event.description = "Track projection"
        mock_event.severity = SeverityLevel.SEVERE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [85.0, 16.0]}  # Observed center
        mock_event.affected_area = None
        mock_event.started_at = now - timedelta(hours=1)
        mock_event.expires_at = None
        mock_event.last_seen_at = now
        mock_event.hazard_observations = []

        mock_report = MagicMock()
        mock_report.data_source = None
        mock_report.source_url = None
        mock_report.external_event_id = None
        mock_report.issued_at = now
        mock_report.fetched_at = now
        mock_report.valid_until = None
        mock_report.raw_payload = {
            "forecast_track": [
                {"waypoint_index": 0, "latitude": 16.0, "longitude": 85.0, "position_type": "current"},
                {"waypoint_index": 1, "latitude": 17.5, "longitude": 85.8, "position_type": "forecast"},
                {"waypoint_index": 2, "latitude": 19.0, "longitude": 86.5, "position_type": "forecast"},
            ]
        }
        mock_event.source_reports = [mock_report]

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 100000.0, False)],
            [],
            [],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=17.0,
            longitude=83.0,
            search_radius_km=500.0,
            lookback_days=7,
        )

        # Observed center remains (16.0, 85.0)
        self.assertIsNotNone(response.latest_observed_position)
        self.assertEqual(response.latest_observed_position.latitude, 16.0)
        self.assertEqual(response.latest_observed_position.longitude, 85.0)
        self.assertEqual(response.latest_observed_position.position_type, "OBSERVED")

        # Forecast waypoints are stored distinctly
        self.assertEqual(len(response.forecast_track_waypoints), 3)
        self.assertEqual(response.forecast_track_waypoints[1].position_type, "FORECAST")
        self.assertEqual(response.forecast_track_waypoints[1].latitude, 17.5)

    def test_missing_measurements_preserved_as_none_never_zero(self) -> None:
        """Verifies that missing wind and surge measurements strictly remain None and are not converted to 0.0."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "Cyclonic Disturbance with Unrecorded Sensor Values"
        mock_event.description = "No direct anemometer readings"
        mock_event.severity = SeverityLevel.MODERATE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [80.5, 13.5]}
        mock_event.affected_area = None
        mock_event.started_at = now
        mock_event.expires_at = None
        mock_event.last_seen_at = now
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 50000.0, False)],
            [],
            [],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=13.08,
            longitude=80.27,
            search_radius_km=500.0,
            lookback_days=7,
        )

        # Missing values must be None, NOT 0.0
        self.assertIsNone(response.wind_hazard.max_sustained_wind_kmph)
        self.assertIsNone(response.wind_hazard.gust_speed_kmph)
        self.assertIsNone(response.storm_surge.estimated_surge_height_m)
        self.assertIsNone(response.storm_surge.peak_wave_height_m)

        # Missing information notifications
        self.assertTrue(any("wind speed observation is unavailable" in msg for msg in response.missing_information))
        self.assertTrue(any("storm surge height measurement is unavailable" in msg for msg in response.missing_information))

    def test_assess_cyclone_hazard_stale_data(self) -> None:
        """Verifies DATA_STALE status when only expired/historical records exist."""
        old_time = datetime.now(timezone.utc) - timedelta(days=5)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "Past Cyclone System"
        mock_event.description = "Dissipated cyclonic storm"
        mock_event.severity = SeverityLevel.MODERATE
        mock_event.status = CrisisStatus.EXPIRED
        mock_event.location = {"type": "Point", "coordinates": [85.0, 18.0]}
        mock_event.affected_area = None
        mock_event.started_at = old_time
        mock_event.expires_at = old_time + timedelta(hours=6)
        mock_event.last_seen_at = old_time
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 80000.0, False)],
            [],
            [],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=18.0,
            longitude=84.5,
            search_radius_km=500.0,
            lookback_days=7,
        )

        self.assertEqual(response.primary_status, CycloneAssessmentStatus.DATA_STALE)
        self.assertIn(CycloneAssessmentStatus.DATA_STALE, response.assessment_statuses)
        self.assertIn("historical or expired", response.summary_message)

    def test_cyclone_hilly_terrain_landslide_concern(self) -> None:
        """Verifies that heavy cyclone precipitation in hilly/mountainous terrain triggers LANDSLIDE_RELATED_CONCERN."""
        now = datetime.now(timezone.utc)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_event.title = "Cyclone Rainband Inundating Western Ghats"
        mock_event.description = "Torrential rains over mountain slopes"
        mock_event.severity = SeverityLevel.SEVERE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [75.0, 13.0]}
        mock_event.affected_area = None
        mock_event.started_at = now
        mock_event.expires_at = None
        mock_event.last_seen_at = now
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        # Create rainfall observation for flood assessment
        mock_rain_obs = MagicMock()
        mock_rain_obs.observation_type = "RAINFALL_DAILY"
        mock_rain_obs.value_numeric = Decimal("145.0")  # Very heavy rain
        mock_rain_obs.unit = "mm"
        mock_rain_obs.properties = {"duration_hours": 24}
        mock_rain_obs.observed_at = now
        mock_rain_obs.data_source = None

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 20000.0, False)],  # Cyclone event
            [],                             # Standalone cyclone obs
            [],                             # Flood events
            [(mock_rain_obs, 5000.0)],      # Flood observations: heavy rain
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        response = assess_cyclone_hazard(
            session=mock_session,
            latitude=13.10,  # Western Ghats mountain ridge
            longitude=75.15,
            search_radius_km=500.0,
            lookback_days=7,
        )

        self.assertIn(CycloneAssessmentStatus.LANDSLIDE_RELATED_CONCERN, response.assessment_statuses)
        self.assertTrue(response.terrain_and_landslide.landslide_concern)
        self.assertTrue(response.terrain_and_landslide.debris_flow_concern)

    def test_api_endpoint_invokes_service(self) -> None:
        """Verifies get_cyclone_assessment endpoint function delegates to assess_cyclone_hazard service."""
        mock_session = MagicMock()
        with patch("app.api.v1.cyclone_assessment.assess_cyclone_hazard") as mock_assess:
            mock_assess.return_value = MagicMock(spec=CycloneAssessmentResponse)
            res = get_cyclone_assessment(
                latitude=19.8,
                longitude=85.8,
                search_radius_km=400.0,
                lookback_days=5,
                session=mock_session,
            )
            mock_assess.assert_called_once_with(
                session=mock_session,
                latitude=19.8,
                longitude=85.8,
                search_radius_km=400.0,
                lookback_days=5,
            )
            self.assertIsNotNone(res)


if __name__ == "__main__":
    unittest.main()
