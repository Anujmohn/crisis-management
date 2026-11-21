import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.main import app
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.services.earthquake_ingestion import (
    map_magnitude_to_severity,
    parse_ncs_html,
    parse_ncs_origin_time,
    sync_earthquakes_from_ncs,
)

SAMPLE_NCS_HTML = """
<!DOCTYPE html>
<html>
<body>
<table class="table table-bordered table-hover" id="archive_eqs">
    <thead>
        <tr>
            <th>Origin Time</th>
            <th>Lat</th>
            <th>Long</th>
            <th>Depth</th>
            <th>Magnitude</th>
            <th>Region</th>
            <th>Location</th>
            <th>Details</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>2026-11-19 23:35:20 IST</td>
            <td>27.086</td>
            <td>92.492</td>
            <td>40</td>
            <td>3.2</td>
            <td>West Kameng, Arunachal Pradesh</td>
            <td>56km NNW of Tezpur, Assam, India</td>
            <td><a href="https://riseq.seismo.gov.in/riseq/earthquake/event/U01meDFNSDhYYkdaVUFNQkVLVSsrZz09/Reviewed" target="_blank">View</a></td>
        </tr>
        <tr>
            <td>2026-11-19 20:49:00 IST</td>
            <td>31.864</td>
            <td>93.763</td>
            <td>10</td>
            <td>5.5</td>
            <td>Tibet</td>
            <td>438km NNW of Pangin, Arunachal Pradesh, India</td>
            <td><a href="https://riseq.seismo.gov.in/riseq/earthquake/event/WDNZWm1pdnYxejdtNkFwNzArb1J2QT09/Preliminary" target="_blank">View</a></td>
        </tr>
        <tr>
            <td>INVALID_TIME</td>
            <td>33.186</td>
            <td>76.180</td>
            <td>10</td>
            <td>2.5</td>
            <td>Kishtwar</td>
            <td>108km N</td>
            <td></td>
        </tr>
        <tr>
            <td>2026-11-19 18:54:05 IST</td>
            <td>999.0</td>
            <td>77.364</td>
            <td>5</td>
            <td>2.7</td>
            <td>Invalid Lat</td>
            <td>Location</td>
            <td></td>
        </tr>
    </tbody>
</table>
</body>
</html>
"""


class EarthquakeIngestionTests(unittest.TestCase):
    def test_parse_ncs_origin_time(self) -> None:
        dt = parse_ncs_origin_time("2026-11-19 23:35:20 IST")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 11)
        self.assertEqual(dt.day, 19)
        # 23:35:20 IST (-5:30) = 18:05:20 UTC
        self.assertEqual(dt.hour, 18)
        self.assertEqual(dt.minute, 5)
        self.assertEqual(dt.second, 20)
        self.assertEqual(dt.tzinfo, timezone.utc)

        # Invalid string
        self.assertIsNone(parse_ncs_origin_time("not-a-timestamp"))

    def test_map_magnitude_to_severity(self) -> None:
        self.assertIsNone(map_magnitude_to_severity(None))
        self.assertEqual(map_magnitude_to_severity(3.2), SeverityLevel.LOW)
        self.assertEqual(map_magnitude_to_severity(4.5), SeverityLevel.MODERATE)
        self.assertEqual(map_magnitude_to_severity(5.5), SeverityLevel.HIGH)
        self.assertEqual(map_magnitude_to_severity(6.2), SeverityLevel.SEVERE)
        self.assertEqual(map_magnitude_to_severity(7.5), SeverityLevel.EXTREME)

    def test_parse_ncs_html(self) -> None:
        records, errors = parse_ncs_html(SAMPLE_NCS_HTML)
        # 2 valid records, 2 invalid rows (invalid time, out-of-bounds latitude)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(errors), 2)

        first = records[0]
        self.assertEqual(first.latitude, 27.086)
        self.assertEqual(first.longitude, 92.492)
        self.assertEqual(first.depth_km, 40.0)
        self.assertEqual(first.magnitude, 3.2)
        self.assertEqual(first.region, "West Kameng, Arunachal Pradesh")
        self.assertEqual(first.external_event_id, "U01meDFNSDhYYkdaVUFNQkVLVSsrZz09")
        self.assertEqual(first.status, "Reviewed")

        second = records[1]
        self.assertEqual(second.magnitude, 5.5)
        self.assertEqual(second.external_event_id, "WDNZWm1pdnYxejdtNkFwNzArb1J2QT09")
        self.assertEqual(second.status, "Preliminary")

    @patch("app.services.earthquake_ingestion.sync_data_sources")
    @patch("app.services.earthquake_ingestion.geojson_to_postgis")
    def test_sync_earthquakes_idempotency_and_duplicates(
        self, mock_geojson, mock_sync_ds
    ) -> None:
        mock_geojson.return_value = MagicMock()

        # Mock database session and DataSource
        mock_session = MagicMock()
        mock_data_source = MagicMock()
        mock_data_source.id = MagicMock()

        # Queries in loop:
        # 1. select DataSource NCS_SEISMO -> returns mock_data_source
        # 2. select CrisisSourceReport for event 1 -> None (creates)
        # 3. select CrisisSourceReport for event 2 -> None (creates)
        mock_session.scalar.side_effect = [
            mock_data_source,
            None,
            None,
        ]

        result = sync_earthquakes_from_ncs(
            session=mock_session,
            html_content=SAMPLE_NCS_HTML,
            max_records=10,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.source, "NCS")
        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.created, 2)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.failed, 2)  # 2 malformed rows in sample HTML
        self.assertTrue(mock_session.commit.called)

    def test_fastapi_route_registered(self) -> None:
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/ingestion/earthquakes/sync", paths)
        self.assertIn("post", paths["/api/v1/ingestion/earthquakes/sync"])


if __name__ == "__main__":
    unittest.main()
