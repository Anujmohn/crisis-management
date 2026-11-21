import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.services.flood_cwc import sync_cwc_flood_data
from app.services.flood_imd import parse_imd_timestamps, sync_imd_flood_data
from app.services.flood_ingestion import sync_all_flood_sources
from app.services.flood_sachet import parse_iso_datetime, sync_sachet_flood_alerts

SAMPLE_IMD_WFS_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "id": 101,
                "District": "SRIKAKULAM",
                "State": "ANDHRA PRADESH",
                "Date": "2026-11-20",
                "toi": "0100",
                "vupto": "0400",
                "Color": 4,  # Red warning
                "cat12": 1,  # Heavy rain >15 mm/hr
                "message": "Extremely heavy rainfall expected",
            },
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[83.8, 18.2], [84.2, 18.2], [84.2, 18.6], [83.8, 18.6], [83.8, 18.2]]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "id": 102,
                "District": "PUNE",
                "State": "MAHARASHTRA",
                "Date": "2026-11-20",
                "toi": "0100",
                "vupto": "0400",
                "Color": 1,  # Green / No Warning
                "cat1": 1,
                "cat12": 0,
            },
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[73.8, 18.5], [74.0, 18.5], [74.0, 18.7], [73.8, 18.7], [73.8, 18.5]]]
                ],
            },
        },
    ],
}

SAMPLE_SACHET_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>NDMA SACHET Disaster Warnings</title>
    <item>
      <title>Extremely Heavy Rain and Flash Flood warning for Srikakulam</title>
      <category>Met</category>
      <link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=TEST_ALERT_1001</link>
      <pubDate>Thu, 19 Nov 2026 18:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

SAMPLE_CAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>TEST_ALERT_1001</identifier>
  <sender>IMD-Visakhapatnam</sender>
  <sent>2026-11-19T22:30:00+05:30</sent>
  <status>Actual</status>
  <msgType>Alert</msgType>
  <scope>Public</scope>
  <info>
    <category>Met</category>
    <event>Flash Flood</event>
    <urgency>Immediate</urgency>
    <severity>Extreme</severity>
    <certainty>Likely</certainty>
    <effective>2026-11-19T22:30:00+05:30</effective>
    <expires>2026-11-20T22:30:00+05:30</expires>
    <headline>Flash Flood Warning for Srikakulam district</headline>
    <description>Heavy inundation expected in low lying areas.</description>
    <instruction>Move to higher ground and avoid crossing flooded roads.</instruction>
    <area>
      <areaDesc>Srikakulam, Andhra Pradesh</areaDesc>
    </area>
  </info>
</alert>
"""


class FloodIngestionTests(unittest.TestCase):
    def test_parse_imd_timestamps(self) -> None:
        t_issue, t_valid = parse_imd_timestamps("2026-11-20", "0100", "0400")
        self.assertEqual(t_issue.tzinfo, timezone.utc)
        self.assertEqual(t_valid.tzinfo, timezone.utc)
        self.assertTrue(t_valid > t_issue)

    def test_parse_iso_datetime(self) -> None:
        dt = parse_iso_datetime("2026-11-19T22:30:00+05:30")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.day, 19)
        self.assertEqual(dt.tzinfo, timezone.utc)

    @patch("app.services.flood_cwc.probe_cwc_service")
    @patch("app.services.flood_cwc.sync_data_sources")
    def test_cwc_adapter_reports_unavailable_when_offline(
        self, mock_sync_ds, mock_probe
    ) -> None:
        mock_probe.return_value = (False, "CWC portal HTTP Error 502: Bad Gateway")
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_session.scalar.return_value = mock_ds

        summary = sync_cwc_flood_data(session=mock_session)
        self.assertEqual(summary.status, "UNAVAILABLE")
        self.assertIn("502", summary.message)
        self.assertEqual(summary.created, 0)

    @patch("app.services.flood_cwc.geojson_to_postgis")
    @patch("app.services.flood_cwc.sync_data_sources")
    def test_cwc_adapter_ingests_observations_idempotently(
        self, mock_sync_ds, mock_geojson
    ) -> None:
        mock_geojson.return_value = MagicMock()
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()
        mock_session.scalar.return_value = mock_ds
        mock_session.scalars.return_value.all.return_value = []

        sample_observations = [
            {
                "station_code": "CWC_STN_001",
                "station_name": "Tezpur Gauge",
                "river_name": "Brahmaputra",
                "basin": "Brahmaputra",
                "latitude": 26.6,
                "longitude": 92.8,
                "water_level": 65.25,
                "warning_level": 64.0,
                "danger_level": 65.5,
                "observed_at": "2026-11-20T00:00:00+00:00",
                "unit": "m",
            }
        ]

        summary = sync_cwc_flood_data(session=mock_session, observations_data=sample_observations)
        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.fetched, 1)
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(summary.failed, 0)
        self.assertTrue(mock_session.commit.called)

    @patch("app.services.flood_imd.geojson_to_postgis")
    @patch("app.services.flood_imd.sync_data_sources")
    def test_imd_adapter_creates_event_for_warning_and_skips_green(
        self, mock_sync_ds, mock_geojson
    ) -> None:
        mock_geojson.return_value = MagicMock()
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()

        # DataSource found, no existing CrisisSourceReport (None)
        mock_session.scalar.side_effect = [
            mock_ds,
            None,
        ]

        summary = sync_imd_flood_data(
            session=mock_session,
            geojson_data=SAMPLE_IMD_WFS_GEOJSON,
        )

        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.fetched, 2)
        # 1 warning district (Srikakulam, Red/cat12) created, 1 calm district (Pune, Green) skipped
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failed, 0)
        self.assertTrue(mock_session.commit.called)

    @patch("app.services.flood_sachet.fetch_sachet_xml")
    @patch("app.services.flood_sachet.sync_data_sources")
    def test_sachet_adapter_parses_cap_alert(
        self, mock_sync_ds, mock_fetch_xml
    ) -> None:
        mock_session = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = MagicMock()
        mock_session.scalar.side_effect = [
            mock_ds,
            None,
        ]
        # First call fetches alert XML
        mock_fetch_xml.return_value = SAMPLE_CAP_XML

        summary = sync_sachet_flood_alerts(
            session=mock_session,
            rss_xml_content=SAMPLE_SACHET_RSS,
        )

        self.assertEqual(summary.status, "ACTIVE")
        self.assertEqual(summary.fetched, 1)
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.skipped, 0)
        self.assertTrue(mock_session.commit.called)

    @patch("app.services.flood_ingestion.sync_cwc_flood_data")
    @patch("app.services.flood_ingestion.sync_imd_flood_data")
    @patch("app.services.flood_ingestion.sync_sachet_flood_alerts")
    def test_orchestrator_combines_sources(
        self, mock_sachet, mock_imd, mock_cwc
    ) -> None:
        mock_session = MagicMock()
        from app.schemas.ingestion import SourceSyncSummary

        mock_cwc.return_value = SourceSyncSummary(status="UNAVAILABLE", message="502 Bad Gateway")
        mock_imd.return_value = SourceSyncSummary(status="ACTIVE", fetched=10, created=2)
        mock_sachet.return_value = SourceSyncSummary(status="ACTIVE", fetched=3, created=1)

        result = sync_all_flood_sources(session=mock_session)
        self.assertTrue(result.success)
        self.assertIn("CWC", result.sources)
        self.assertIn("IMD", result.sources)
        self.assertIn("NDMA_SACHET", result.sources)
        self.assertEqual(result.sources["CWC"].status, "UNAVAILABLE")
        self.assertEqual(result.sources["IMD"].status, "ACTIVE")

    def test_floods_sync_route_registered(self) -> None:
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/ingestion/floods/sync", paths)
        self.assertIn("post", paths["/api/v1/ingestion/floods/sync"])


if __name__ == "__main__":
    unittest.main()
