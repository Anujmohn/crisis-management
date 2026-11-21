from __future__ import annotations

import math
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.earthquake_assessment import get_earthquake_assessment
from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.earthquake_assessment import (
    AssessmentStatus,
    DepthClassification,
    EarthquakeAssessmentResponse,
)
from app.services.earthquake_assessment import (
    assess_earthquake_hazard,
    calculate_haversine_distance_km,
    classify_focal_depth,
)


class EarthquakeAssessmentTests(unittest.TestCase):
    def test_earthquake_assessment_route_registered(self) -> None:
        """Verifies GET /api/v1/earthquake/assessment is registered in OpenAPI schema with correct parameter constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/earthquake/assessment", paths)
        self.assertIn("get", paths["/api/v1/earthquake/assessment"])

        # Check parameter definitions and constraints
        params = {p["name"]: p for p in paths["/api/v1/earthquake/assessment"]["get"]["parameters"]}
        self.assertIn("latitude", params)
        self.assertIn("longitude", params)
        self.assertIn("lookback_days", params)
        self.assertIn("search_radius_km", params)

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

        # Lookback days bounds
        lookback_schema = params["lookback_days"]["schema"]
        self.assertEqual(lookback_schema.get("minimum"), 1)
        self.assertEqual(lookback_schema.get("maximum"), 365)
        self.assertEqual(lookback_schema.get("default"), 30)

        # Search radius bounds
        radius_schema = params["search_radius_km"]["schema"]
        self.assertEqual(radius_schema.get("maximum"), 1000.0)
        self.assertEqual(radius_schema.get("default"), 300.0)

    def test_haversine_and_depth_classification(self) -> None:
        """Verifies distance and focal depth classification logic."""
        # Distance from (0, 0) to (0, 1) ~ 111.19 km
        dist = calculate_haversine_distance_km(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(dist, 111.19, delta=1.0)

        # Shallow depth (< 70 km)
        class_shallow, note_shallow = classify_focal_depth(10.0)
        self.assertEqual(class_shallow, DepthClassification.SHALLOW.value)
        self.assertIn("< 70 km", note_shallow)

        # Intermediate depth (70 - 300 km)
        class_inter, note_inter = classify_focal_depth(150.0)
        self.assertEqual(class_inter, DepthClassification.INTERMEDIATE.value)
        self.assertIn("70–300 km", note_inter)

        # Deep (> 300 km)
        class_deep, note_deep = classify_focal_depth(450.0)
        self.assertEqual(class_deep, DepthClassification.DEEP.value)
        self.assertIn("> 300 km", note_deep)

        # Unknown depth
        class_unknown, note_unknown = classify_focal_depth(None)
        self.assertEqual(class_unknown, DepthClassification.UNKNOWN.value)
        self.assertIn("not recorded", note_unknown)

    def test_assess_earthquake_hazard_no_relevant_events(self) -> None:
        """Verifies NO_RELEVANT_EVENT_FOUND status and mandatory disclaimer."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []

        response = assess_earthquake_hazard(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            lookback_days=30,
            search_radius_km=300.0,
        )

        self.assertEqual(response.assessment_status, AssessmentStatus.NO_RELEVANT_EVENT_FOUND.value)
        self.assertEqual(response.events_analyzed_count, 0)
        self.assertEqual(len(response.relevant_events), 0)
        # Verify required safety disclaimer
        self.assertIn(
            "No relevant earthquake event was found in the available dataset",
            response.summary_message,
        )
        self.assertIn(
            "This does not establish that the location is safe.",
            response.summary_message,
        )
        self.assertTrue(len(response.scientific_limitations) > 0)

    def test_assess_earthquake_hazard_with_events(self) -> None:
        """Verifies POTENTIAL_RELEVANT_EVENT status, distance, depth, and hypocentral calculations."""
        now = datetime.now(timezone.utc)
        user_lat, user_lon = 28.6139, 77.2090

        # Event: epicenter ~ 35 km away, depth = 10 km, magnitude = 4.5
        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.EARTHQUAKE
        mock_event.title = "M 4.5 - Northern India"
        mock_event.description = "Earthquake recorded by NCS"
        mock_event.severity = SeverityLevel.MODERATE
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [77.5000, 28.8000]}
        mock_event.started_at = now - timedelta(hours=2)
        mock_event.expires_at = None
        mock_event.first_seen_at = now - timedelta(hours=2)
        mock_event.last_seen_at = now - timedelta(minutes=15)

        # Mock source report
        mock_source = MagicMock()
        mock_source.data_source = MagicMock()
        mock_source.data_source.name = "National Center for Seismology"
        mock_source.data_source.source_type = "PUBLIC_PORTAL"
        mock_source.source_url = "https://riseq.seismo.gov.in"
        mock_source.external_event_id = "NCS_20261120_001"
        mock_source.issued_at = now - timedelta(hours=2)
        mock_source.fetched_at = now - timedelta(minutes=15)
        mock_source.valid_until = None
        mock_source.raw_payload = {
            "latitude": 28.8000,
            "longitude": 77.5000,
            "magnitude": 4.5,
            "depth_km": 10.0,
            "region": "Northern India",
        }
        mock_source.source_severity = "4.5"
        mock_source.source_description = "Northern India"
        mock_event.source_reports = [mock_source]

        # Mock hazard observations
        mock_obs = MagicMock()
        mock_obs.observation_type = "EARTHQUAKE_MAGNITUDE"
        mock_obs.value_numeric = Decimal("4.5")
        mock_obs.value_text = "4.5 M"
        mock_obs.properties = {"depth_km": 10.0}
        mock_event.hazard_observations = [mock_obs]

        dist_meters = 35000.0  # 35 km
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [(mock_event, dist_meters)]

        response = assess_earthquake_hazard(
            session=mock_session,
            latitude=user_lat,
            longitude=user_lon,
            lookback_days=14,
            search_radius_km=200.0,
        )

        self.assertEqual(response.assessment_status, AssessmentStatus.POTENTIAL_RELEVANT_EVENT.value)
        self.assertEqual(response.events_analyzed_count, 1)
        self.assertEqual(len(response.relevant_events), 1)

        event_res = response.relevant_events[0]
        self.assertEqual(event_res.magnitude, 4.5)
        self.assertEqual(event_res.depth_km, 10.0)
        self.assertEqual(event_res.depth_classification, DepthClassification.SHALLOW.value)
        self.assertEqual(event_res.epicentral_distance_km, 35.0)

        # Hypocentral distance = sqrt(35^2 + 10^2) = sqrt(1225 + 100) = sqrt(1325) ~ 36.40 km
        expected_hypo = round(math.sqrt(35.0**2 + 10.0**2), 2)
        self.assertEqual(event_res.hypocentral_distance_km, expected_hypo)
        self.assertEqual(event_res.data_completeness, "COMPLETE")
        self.assertEqual(event_res.freshness, "FRESH")

    def test_assess_earthquake_hazard_missing_depth_and_magnitude(self) -> None:
        """Verifies handling and warnings when focal depth or magnitude is unrecorded."""
        now = datetime.now(timezone.utc)
        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.EARTHQUAKE
        mock_event.title = "Unassigned Seismic Event"
        mock_event.description = "Preliminary unreviewed event"
        mock_event.severity = None
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [78.0, 29.0]}
        mock_event.started_at = now - timedelta(hours=1)
        mock_event.expires_at = None
        mock_event.first_seen_at = now - timedelta(hours=1)
        mock_event.last_seen_at = now - timedelta(hours=1)
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [(mock_event, 50000.0)]

        response = assess_earthquake_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            lookback_days=7,
            search_radius_km=100.0,
        )

        event_res = response.relevant_events[0]
        self.assertIsNone(event_res.magnitude)
        self.assertIsNone(event_res.depth_km)
        self.assertIsNone(event_res.hypocentral_distance_km)
        self.assertEqual(event_res.depth_classification, DepthClassification.UNKNOWN.value)
        self.assertEqual(event_res.data_completeness, "MINIMAL")
        self.assertTrue(len(response.missing_data_warnings) > 0)

    def test_assess_earthquake_hazard_stale_data(self) -> None:
        """Verifies DATA_STALE status when recorded events are past freshness thresholds."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=20)

        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.EARTHQUAKE
        mock_event.title = "Past Historical Earthquake"
        mock_event.description = "Past recorded event"
        mock_event.severity = SeverityLevel.LOW
        mock_event.status = CrisisStatus.EXPIRED
        mock_event.location = {"type": "Point", "coordinates": [77.5, 28.5]}
        mock_event.started_at = old_time
        mock_event.expires_at = old_time + timedelta(hours=48)
        mock_event.first_seen_at = old_time
        mock_event.last_seen_at = old_time
        mock_event.source_reports = []
        mock_event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [(mock_event, 40000.0)]

        response = assess_earthquake_hazard(
            session=mock_session,
            latitude=28.6,
            longitude=77.2,
            lookback_days=30,
            search_radius_km=100.0,
        )

        self.assertEqual(response.assessment_status, AssessmentStatus.DATA_STALE.value)

    def test_api_endpoint_invokes_service(self) -> None:
        """Verifies get_earthquake_assessment endpoint function delegates to service."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []

        res = get_earthquake_assessment(
            latitude=28.6139,
            longitude=77.2090,
            lookback_days=30,
            search_radius_km=300.0,
            session=mock_session,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.assessment_status, AssessmentStatus.NO_RELEVANT_EVENT_FOUND.value)
        self.assertEqual(res.user_location.latitude, 28.6139)
        self.assertEqual(res.user_location.longitude, 77.2090)


if __name__ == "__main__":
    unittest.main()
