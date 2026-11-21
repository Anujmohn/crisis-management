import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from fastapi import HTTPException

from app.api.v1.disasters import parse_crisis_statuses, parse_disaster_types
from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.services.disaster_search import (
    evaluate_freshness,
    get_nearby_disasters,
    safe_postgis_to_geojson,
)


class DisasterNearbyApiTests(unittest.TestCase):
    def test_nearby_route_registered_in_openapi(self) -> None:
        """Verifies GET /api/v1/disasters/nearby is registered in OpenAPI schema."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/disasters/nearby", paths)
        self.assertIn("get", paths["/api/v1/disasters/nearby"])

        # Check parameter definitions
        params = {p["name"]: p for p in paths["/api/v1/disasters/nearby"]["get"]["parameters"]}
        self.assertIn("latitude", params)
        self.assertIn("longitude", params)
        self.assertIn("radius_km", params)
        self.assertIn("disaster_types", params)
        self.assertIn("status", params)
        self.assertIn("limit", params)

    def test_parse_disaster_types_valid(self) -> None:
        result = parse_disaster_types("earthquake,flood,cyclone")
        self.assertEqual(
            result,
            [CrisisType.EARTHQUAKE, CrisisType.FLOOD, CrisisType.CYCLONE],
        )

        single = parse_disaster_types("EARTHQUAKE")
        self.assertEqual(single, [CrisisType.EARTHQUAKE])

        empty = parse_disaster_types("")
        self.assertIsNone(empty)

        none_val = parse_disaster_types(None)
        self.assertIsNone(none_val)

    def test_parse_disaster_types_invalid(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            parse_disaster_types("earthquake,tornado")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid disaster type 'tornado'", ctx.exception.detail)

    def test_parse_crisis_statuses_valid(self) -> None:
        result = parse_crisis_statuses("active,expired")
        self.assertEqual(result, [CrisisStatus.ACTIVE, CrisisStatus.EXPIRED])

        single = parse_crisis_statuses("ACTIVE")
        self.assertEqual(single, [CrisisStatus.ACTIVE])

        none_val = parse_crisis_statuses(None)
        self.assertIsNone(none_val)

    def test_parse_crisis_statuses_invalid(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            parse_crisis_statuses("ACTIVE,RESOLVED")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid status 'RESOLVED'", ctx.exception.detail)

    def test_evaluate_freshness_states(self) -> None:
        now = datetime.now(timezone.utc)

        # 1. Status EXPIRED
        event_expired_status = MagicMock()
        event_expired_status.status = CrisisStatus.EXPIRED
        self.assertEqual(evaluate_freshness(event_expired_status, now), "EXPIRED")

        # 2. Past expires_at
        event_expired_time = MagicMock()
        event_expired_time.status = CrisisStatus.ACTIVE
        event_expired_time.expires_at = now - timedelta(hours=1)
        self.assertEqual(evaluate_freshness(event_expired_time, now), "EXPIRED")

        # 3. Future expires_at -> FRESH
        event_fresh_time = MagicMock()
        event_fresh_time.status = CrisisStatus.ACTIVE
        event_fresh_time.expires_at = now + timedelta(hours=4)
        self.assertEqual(evaluate_freshness(event_fresh_time, now), "FRESH")

        # 4. Source report with future valid_until -> FRESH
        event_report_valid = MagicMock()
        event_report_valid.status = CrisisStatus.ACTIVE
        event_report_valid.expires_at = None
        report = MagicMock()
        report.valid_until = now + timedelta(hours=2)
        event_report_valid.source_reports = [report]
        self.assertEqual(evaluate_freshness(event_report_valid, now), "FRESH")

        # 5. Recently updated (< 6 hours) -> FRESH
        event_recent = MagicMock()
        event_recent.status = CrisisStatus.ACTIVE
        event_recent.expires_at = None
        event_recent.source_reports = []
        event_recent.last_seen_at = now - timedelta(minutes=45)
        self.assertEqual(evaluate_freshness(event_recent, now), "FRESH")

        # 6. Older update (> 6 hours, no validity) -> STALE
        event_stale = MagicMock()
        event_stale.status = CrisisStatus.ACTIVE
        event_stale.expires_at = None
        event_stale.source_reports = []
        event_stale.last_seen_at = now - timedelta(hours=18)
        self.assertEqual(evaluate_freshness(event_stale, now), "STALE")

    def test_safe_postgis_to_geojson(self) -> None:
        # None
        self.assertIsNone(safe_postgis_to_geojson(None))

        # Dict (already GeoJSON)
        point_dict = {"type": "Point", "coordinates": [85.3, 14.5]}
        self.assertEqual(safe_postgis_to_geojson(point_dict), point_dict)

    def test_get_nearby_disasters_with_point_and_polygon_events(self) -> None:
        now = datetime.now(timezone.utc)

        # Event 1: Earthquake with point geometry only
        eq_event = MagicMock()
        eq_event.id = uuid.uuid4()
        eq_event.crisis_type = CrisisType.EARTHQUAKE
        eq_event.title = "M 4.2 Earthquake - Bay of Bengal"
        eq_event.description = "Reviewed earthquake event from NCS"
        eq_event.severity = SeverityLevel.MODERATE
        eq_event.status = CrisisStatus.ACTIVE
        eq_event.location = {"type": "Point", "coordinates": [84.5, 17.2]}
        eq_event.affected_area = None
        eq_event.started_at = now - timedelta(hours=1)
        eq_event.expires_at = None
        eq_event.first_seen_at = now - timedelta(hours=1)
        eq_event.last_seen_at = now - timedelta(minutes=30)

        eq_source = MagicMock()
        eq_source.data_source = MagicMock()
        eq_source.data_source.name = "National Center for Seismology"
        eq_source.data_source.source_type = "PUBLIC_PORTAL"
        eq_source.source_url = "https://riseq.seismo.gov.in"
        eq_source.external_event_id = "NCS_20261120_001"
        eq_source.issued_at = now - timedelta(hours=1)
        eq_source.fetched_at = now - timedelta(minutes=30)
        eq_source.valid_until = None
        eq_event.source_reports = [eq_source]

        eq_obs = MagicMock()
        eq_obs.id = uuid.uuid4()
        eq_obs.observation_type = "EARTHQUAKE_MAGNITUDE"
        eq_obs.value_numeric = Decimal("4.2")
        eq_obs.value_text = "4.2 M"
        eq_obs.unit = "magnitude"
        eq_obs.geometry = None
        eq_obs.observed_at = now - timedelta(hours=1)
        eq_obs.properties = {"depth_km": 10.0}
        eq_event.hazard_observations = [eq_obs]

        # Event 2: Flood with affected_area polygon only (no point)
        flood_event = MagicMock()
        flood_event.id = uuid.uuid4()
        flood_event.crisis_type = CrisisType.FLOOD
        flood_event.title = "IMD Orange Alert: Heavy Rain - Srikakulam"
        flood_event.description = "Flash flood advisory for district"
        flood_event.severity = SeverityLevel.SEVERE
        flood_event.status = CrisisStatus.ACTIVE
        flood_event.location = None
        flood_event.affected_area = {
            "type": "MultiPolygon",
            "coordinates": [[[[83.8, 18.2], [84.2, 18.2], [84.2, 18.6], [83.8, 18.6], [83.8, 18.2]]]],
        }
        flood_event.started_at = now
        flood_event.expires_at = now + timedelta(hours=3)
        flood_event.first_seen_at = now
        flood_event.last_seen_at = now
        flood_event.source_reports = []
        flood_event.hazard_observations = []

        # Event 3: Cyclone with both location point and affected_area corridor
        cyclone_event = MagicMock()
        cyclone_event.id = uuid.uuid4()
        cyclone_event.crisis_type = CrisisType.CYCLONE
        cyclone_event.title = "Severe Cyclonic Storm AKVAN"
        cyclone_event.description = "SCS Akvan with maximum wind speed of 90-100 gusting to 110 kmph"
        cyclone_event.severity = SeverityLevel.EXTREME
        cyclone_event.status = CrisisStatus.ACTIVE
        cyclone_event.location = {"type": "Point", "coordinates": [85.3, 14.5]}
        cyclone_event.affected_area = {
            "type": "MultiPolygon",
            "coordinates": [[[[82.0, 14.0], [86.0, 14.0], [86.0, 16.0], [82.0, 16.0], [82.0, 14.0]]]],
        }
        cyclone_event.started_at = now
        cyclone_event.expires_at = now + timedelta(hours=6)
        cyclone_event.first_seen_at = now
        cyclone_event.last_seen_at = now

        cyc_obs = MagicMock()
        cyc_obs.id = uuid.uuid4()
        cyc_obs.observation_type = "CYCLONE_MAX_WIND_SPEED"
        cyc_obs.value_numeric = Decimal("100.0")
        cyc_obs.value_text = "90-100 kmph"
        cyc_obs.unit = "kmph"
        cyc_obs.geometry = None
        cyc_obs.observed_at = now
        cyc_obs.properties = {"gust": "110 kmph"}
        cyclone_event.hazard_observations = [cyc_obs]
        cyclone_event.source_reports = []

        # Mock session execute results: rows of (event, distance_meters)
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [
            (eq_event, 12500.0),      # 12.5 km
            (flood_event, 24800.0),   # 24.8 km
            (cyclone_event, 45200.0), # 45.2 km
        ]

        response = get_nearby_disasters(
            session=mock_session,
            latitude=17.5,
            longitude=84.0,
            radius_km=50.0,
        )

        self.assertTrue(response.success)
        self.assertEqual(response.user_location.latitude, 17.5)
        self.assertEqual(response.user_location.longitude, 84.0)
        self.assertEqual(response.search_radius_km, 50.0)
        self.assertEqual(response.total_events, 3)

        # Verify Event 1 (Earthquake)
        e1 = response.events[0]
        self.assertEqual(e1.disaster_type, CrisisType.EARTHQUAKE)
        self.assertEqual(e1.distance_km, 12.5)
        self.assertIsNotNone(e1.location)
        self.assertIsNone(e1.affected_area)
        self.assertEqual(len(e1.sources), 1)
        self.assertEqual(e1.sources[0].source_name, "National Center for Seismology")
        self.assertEqual(len(e1.observations), 1)
        self.assertEqual(e1.observations[0].observation_type, "EARTHQUAKE_MAGNITUDE")
        self.assertEqual(e1.freshness, "FRESH")

        # Verify Event 2 (Flood - Polygon only, ensures not ignored)
        e2 = response.events[1]
        self.assertEqual(e2.disaster_type, CrisisType.FLOOD)
        self.assertEqual(e2.distance_km, 24.8)
        self.assertIsNone(e2.location)
        self.assertIsNotNone(e2.affected_area)
        self.assertEqual(e2.freshness, "FRESH")

        # Verify Event 3 (Cyclone - Both location and polygon)
        e3 = response.events[2]
        self.assertEqual(e3.disaster_type, CrisisType.CYCLONE)
        self.assertEqual(e3.severity, SeverityLevel.EXTREME)
        self.assertIsNotNone(e3.location)
        self.assertIsNotNone(e3.affected_area)
        self.assertEqual(len(e3.observations), 1)
        self.assertEqual(e3.observations[0].observation_type, "CYCLONE_MAX_WIND_SPEED")

    def test_get_nearby_disasters_empty_result(self) -> None:
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []

        response = get_nearby_disasters(
            session=mock_session,
            latitude=28.61,
            longitude=77.20,
            radius_km=30.0,
        )

        self.assertTrue(response.success)
        self.assertEqual(response.total_events, 0)
        self.assertEqual(len(response.events), 0)

    def test_missing_source_information_handled_gracefully(self) -> None:
        now = datetime.now(timezone.utc)
        event = MagicMock()
        event.id = uuid.uuid4()
        event.crisis_type = CrisisType.FLOOD
        event.title = "Local Flood Alert"
        event.description = None
        event.severity = None
        event.status = CrisisStatus.ACTIVE
        event.location = None
        event.affected_area = {
            "type": "MultiPolygon",
            "coordinates": [[[[80.0, 20.0], [80.5, 20.0], [80.5, 20.5], [80.0, 20.5], [80.0, 20.0]]]],
        }
        event.started_at = None
        event.expires_at = None
        event.first_seen_at = now
        event.last_seen_at = now

        # Source report with missing data_source relation
        orphan_report = MagicMock()
        orphan_report.data_source = None
        orphan_report.source_url = None
        orphan_report.external_event_id = None
        orphan_report.issued_at = None
        orphan_report.fetched_at = now
        orphan_report.valid_until = None
        event.source_reports = [orphan_report]
        event.hazard_observations = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = [(event, 5000.0)]

        response = get_nearby_disasters(
            session=mock_session,
            latitude=20.0,
            longitude=80.0,
            radius_km=25.0,
        )
        self.assertEqual(response.total_events, 1)
        src = response.events[0].sources[0]
        self.assertEqual(src.source_name, "Unknown Official Source")
        self.assertEqual(src.source_type, "UNKNOWN")

    def test_get_nearby_disaster_events_endpoint_calls_service(self) -> None:
        from app.api.v1.disasters import get_nearby_disaster_events

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []

        # 1. Single category filtering
        res_eq = get_nearby_disaster_events(
            latitude=28.0,
            longitude=77.0,
            radius_km=100.0,
            disaster_types="EARTHQUAKE",
            status="ACTIVE",
            limit=25,
            db=mock_session,
        )
        self.assertTrue(res_eq.success)

        # 2. Multiple category filtering
        res_multi = get_nearby_disaster_events(
            latitude=28.0,
            longitude=77.0,
            radius_km=100.0,
            disaster_types="flood,cyclone",
            status="ACTIVE,EXPIRED",
            limit=10,
            db=mock_session,
        )
        self.assertTrue(res_multi.success)


if __name__ == "__main__":
    unittest.main()
