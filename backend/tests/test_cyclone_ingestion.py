import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.services.cyclone_imd import sync_imd_cyclone_data
from app.services.cyclone_incois import sync_incois_coastal_data
from app.services.cyclone_ingestion import sync_all_cyclone_sources
from app.services.cyclone_sachet import (
    extract_center_coordinates,
    extract_wave_height,
    extract_wind_speeds,
    parse_cap_polygon,
    sync_sachet_cyclone_alerts,
)

SAMPLE_IMD_CYCLONE_WFS_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "Cyclone_Track_V.1",
            "properties": {
                "id": 1,
                "cyclone_id": 119,
                "cyclone_type": "forecast",
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [85.2, 16.1],
                    [85.1, 17.1],
                    [85.0, 18.2],
                    [85.0, 19.33],
                    [85.0, 20.4],
                    [85.2, 22.2],
                ],
            },
        }
    ],
}

SAMPLE_SACHET_CYCLONE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>NDMA SACHET Disaster Warnings</title>
    <item>
      <title>SCS AKVAN with maximum wind speed of 90-100 gusting to 110 Kmph lays centred at 0530 IST</title>
      <category>Met</category>
      <link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=TEST_CYCLONE_AKVAN</link>
      <pubDate>Thu, 19 Nov 2026 11:57:34 +05:30</pubDate>
    </item>
  </channel>
</rss>
"""

SAMPLE_CYCLONE_CAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>TEST_CYCLONE_AKVAN</identifier>
  <sender>IMD-Visakhapatnam</sender>
  <sent>2026-11-19T11:57:34+05:30</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
  <info>
    <category>Met</category>
    <event>Cyclone</event>
    <urgency>Immediate</urgency>
    <severity>Extreme</severity>
    <certainty>Possible</certainty>
    <effective>2026-11-19T11:52:00+05:30</effective>
    <expires>2026-11-20T14:52:00+05:30</expires>
    <headline>SCS AKVAN with maximum wind speed of 90-100 gusting to 110 Kmph lays centred at 0530 IST</headline>
    <description>The Severe Cyclonic Storm Akvan over westcentral Bay of Bengal lay centered near Latitude 14.5N and Longitude 85.3E with maximum sustained wind speed of 90-100 kmph gusting to 110 kmph.</description>
    <instruction>Fishermen are advised not to venture into deep sea. Coastal residents should secure thatched roofs.</instruction>
    <area>
      <areaDesc>Coastal Andhra Pradesh and South Odisha</areaDesc>
      <polygon>16.0,82.0 16.0,86.0 20.0,86.0 20.0,82.0 16.0,82.0</polygon>
    </area>
  </info>
</alert>
"""


class CycloneIngestionTests(unittest.TestCase):
    def test_cyclones_sync_route_registered(self) -> None:
        """Verifies POST /api/v1/ingestion/cyclones/sync is registered in OpenAPI schema."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/api/v1/ingestion/cyclones/sync", paths)
        self.assertIn("post", paths["/api/v1/ingestion/cyclones/sync"])

    def test_extract_wind_speeds(self) -> None:
        text = "SCS AKVAN with maximum wind speed of 90-100 gusting to 110 Kmph lays centred over Bay of Bengal"
        sustained_val, sustained_txt, gust_val, gust_txt = extract_wind_speeds(text)
        self.assertEqual(sustained_val, Decimal("100"))
        self.assertIn("kmph", sustained_txt.lower())
        self.assertEqual(gust_val, Decimal("110"))
        self.assertIn("kmph", gust_txt.lower())

    def test_extract_center_coordinates(self) -> None:
        text = "Cyclonic storm lay centered near Latitude 14.5N and Longitude 85.3E about 440 km south-southeast"
        coords = extract_center_coordinates(text)
        self.assertIsNotNone(coords)
        self.assertEqual(coords, (85.3, 14.5))

    def test_extract_wave_height(self) -> None:
        # Telugu script coastal alert
        telugu_text = "నవంబర్ 18 సా.4 గంటల నుండి 20 సా.4 గంటల వరకు సముద్రంలో 1.1-1.3 మీటర్ల ఎత్తులో అలల"
        val, txt = extract_wave_height(telugu_text)
        self.assertEqual(val, Decimal("1.3"))
        self.assertEqual(txt, "1.1-1.3 m")

        # English script coastal alert
        eng_text = "Coastal warning: High waves of 1.4-1.6 meters expected"
        val_eng, txt_eng = extract_wave_height(eng_text)
        self.assertEqual(val_eng, Decimal("1.6"))

    def test_parse_cap_polygon(self) -> None:
        poly_str = "16.0,82.0 16.0,86.0 20.0,86.0 20.0,82.0 16.0,82.0"
        multi_poly = parse_cap_polygon(poly_str)
        self.assertIsNotNone(multi_poly)
        self.assertTrue(multi_poly.is_valid)

    @patch("app.services.cyclone_imd.geojson_to_postgis")
    @patch("app.services.cyclone_imd.sync_data_sources")
    def test_imd_cyclone_adapter_tracks_and_forecast_preservation(
        self, mock_sync_ds, mock_geojson
    ) -> None:
        mock_geojson.return_value = MagicMock()
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()
        mock_session.scalar.return_value = mock_ds

        # First run: new cyclone event created
        mock_session.scalar.side_effect = [mock_ds, None]  # ds, existing_report=None
        summary = sync_imd_cyclone_data(
            session=mock_session,
            geojson_data=SAMPLE_IMD_CYCLONE_WFS_GEOJSON,
            bulletins_data={"national_bulletin_pdf": "https://example.com/bulletin.pdf"},
        )
        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.skipped, 0)

        # Verify added objects
        added = [call.args[0] for call in mock_session.add.call_args_list]
        events = [obj for obj in added if hasattr(obj, "crisis_type")]
        reports = [obj for obj in added if hasattr(obj, "external_event_id")]
        observations = [obj for obj in added if hasattr(obj, "observation_type")]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].crisis_type, CrisisType.CYCLONE)
        self.assertEqual(events[0].severity, SeverityLevel.SEVERE)

        self.assertEqual(len(reports), 1)
        # Step 5 check: raw_payload preserves forecast_track waypoints without overwriting current position
        forecast_track = reports[0].raw_payload.get("forecast_track", [])
        self.assertEqual(len(forecast_track), 6)
        self.assertEqual(forecast_track[0]["position_type"], "current")
        self.assertEqual(forecast_track[1]["position_type"], "forecast")

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observation_type, "CYCLONE_POSITION")

        # Second run: existing report with identical payload hash is skipped idempotently
        existing_report = MagicMock()
        existing_report.payload_hash = reports[0].payload_hash
        existing_report.crisis_event = events[0]
        mock_session.scalar.side_effect = [mock_ds, existing_report]

        summary_repeat = sync_imd_cyclone_data(
            session=mock_session,
            geojson_data=SAMPLE_IMD_CYCLONE_WFS_GEOJSON,
            bulletins_data={"national_bulletin_pdf": "https://example.com/bulletin.pdf"},
        )
        self.assertEqual(summary_repeat.created, 0)
        self.assertEqual(summary_repeat.skipped, 1)

    @patch("app.services.cyclone_sachet.geojson_to_postgis")
    @patch("app.services.cyclone_sachet.sync_data_sources")
    def test_sachet_cyclone_adapter_parses_cap_alert(
        self, mock_sync_ds, mock_geojson
    ) -> None:
        mock_geojson.return_value = MagicMock()
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()

        # First run: new alert
        mock_session.scalar.side_effect = [mock_ds, None]
        summary = sync_sachet_cyclone_alerts(
            session=mock_session,
            rss_xml_content=SAMPLE_SACHET_CYCLONE_RSS,
            cap_xml_map={"TEST_CYCLONE_AKVAN": SAMPLE_CYCLONE_CAP_XML},
        )
        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.created, 1)

        added = [call.args[0] for call in mock_session.add.call_args_list]
        events = [obj for obj in added if hasattr(obj, "crisis_type")]
        obs_types = [obj.observation_type for obj in added if hasattr(obj, "observation_type")]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].crisis_type, CrisisType.CYCLONE)
        self.assertEqual(events[0].severity, SeverityLevel.EXTREME)

        # Wind speed and gust observations were extracted
        self.assertIn("CYCLONE_MAX_WIND_SPEED", obs_types)
        self.assertIn("CYCLONE_GUST_SPEED", obs_types)

    @patch("app.services.cyclone_incois.probe_incois_service")
    @patch("app.services.cyclone_incois.sync_data_sources")
    def test_incois_adapter_reports_unavailable_when_offline(
        self, mock_sync_ds, mock_probe
    ) -> None:
        mock_probe.return_value = (False, "Connection timed out")
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_session.scalar.return_value = mock_ds

        summary = sync_incois_coastal_data(session=mock_session)
        self.assertEqual(summary.status, "UNAVAILABLE")
        self.assertIn("timed out", summary.message)

    @patch("app.services.cyclone_incois.probe_incois_service")
    @patch("app.services.cyclone_incois.sync_data_sources")
    def test_incois_adapter_reports_online_status(
        self, mock_sync_ds, mock_probe
    ) -> None:
        mock_probe.return_value = (True, "INCOIS service reachable.")
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_session.scalar.return_value = mock_ds

        summary = sync_incois_coastal_data(session=mock_session)
        self.assertEqual(summary.status, "PUBLIC_DOWNLOAD_VERIFIED")
        self.assertIn("ERDDAP", summary.message)

    @patch("app.services.cyclone_incois.geojson_to_postgis")
    @patch("app.services.cyclone_incois.sync_data_sources")
    def test_incois_adapter_ingests_observations_idempotently(
        self, mock_sync_ds, mock_geojson
    ) -> None:
        mock_geojson.return_value = MagicMock()
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()
        mock_session.scalar.return_value = mock_ds
        mock_session.scalars.return_value.all.return_value = []

        obs_list = [
            {
                "observation_type": "STORM_SURGE_HEIGHT",
                "value_numeric": "1.85",
                "unit": "m",
                "station_code": "INCOIS_BUOY_04",
                "observed_at": "2026-11-20T01:00:00+00:00",
                "latitude": 17.5,
                "longitude": 83.2,
            }
        ]

        summary = sync_incois_coastal_data(session=mock_session, observations_data=obs_list)
        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.created, 1)

    @patch("app.services.cyclone_ingestion.sync_imd_cyclone_data")
    @patch("app.services.cyclone_ingestion.sync_sachet_cyclone_alerts")
    @patch("app.services.cyclone_ingestion.sync_incois_coastal_data")
    def test_orchestrator_combines_cyclone_sources(
        self, mock_incois, mock_sachet, mock_imd
    ) -> None:
        from app.schemas.ingestion import SourceSyncSummary

        mock_imd.return_value = SourceSyncSummary(status="ACTIVE", fetched=1, created=1)
        mock_sachet.return_value = SourceSyncSummary(status="ACTIVE", fetched=5, created=3)
        mock_incois.return_value = SourceSyncSummary(status="PUBLIC_DOWNLOAD_VERIFIED", fetched=0)

        mock_session = MagicMock()
        response = sync_all_cyclone_sources(session=mock_session)

        self.assertTrue(response.success)
        self.assertIn("IMD", response.sources)
        self.assertIn("SACHET", response.sources)
        self.assertIn("INCOIS", response.sources)
        self.assertEqual(response.sources["IMD"].created, 1)
        self.assertEqual(response.sources["SACHET"].created, 3)


if __name__ == "__main__":
    unittest.main()
