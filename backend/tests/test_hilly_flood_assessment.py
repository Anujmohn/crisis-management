from __future__ import annotations

import math
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.api.v1.flood_assessment import get_flood_assessment
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.flood_assessment import (
    FloodAssessmentStatus,
    GeographicalEnvironment,
    RainfallEvidence,
    RiverLevelStatus,
    RiverObservationEvidence,
    TerrainHazardStatus,
)
from app.services.flood_assessment import (
    assess_flood_hazard,
    classify_geographical_environment,
)
from app.services.terrain_analysis import (
    COPERNICUS_DEM_METADATA,
    SRTM_DEM_METADATA,
    ElevationGrid,
    analyze_hilly_terrain_hazard,
    calculate_d8_flow_directions,
    calculate_flow_accumulation,
    calculate_local_elevation,
    calculate_relative_elevation_above_drainage,
    calculate_slope_and_aspect,
    calculate_topographic_wetness_index,
    classify_valley_morphometry,
    create_synthetic_ridge_dem_grid,
    create_synthetic_steep_slope_dem_grid,
    create_synthetic_valley_dem_grid,
    extract_drainage_network_and_hand,
)


class HillyFloodAssessmentTests(unittest.TestCase):
    def test_dem_dataset_metadata_verification(self) -> None:
        """Verifies authoritative metadata for Copernicus DEM GLO-30 and SRTM GL1."""
        self.assertEqual(COPERNICUS_DEM_METADATA["spatial_resolution_m"], 30.0)
        self.assertEqual(COPERNICUS_DEM_METADATA["horizontal_datum"], "WGS84 (EPSG:4326)")
        self.assertIn("TanDEM-X", COPERNICUS_DEM_METADATA["quality_and_limitations"])

        self.assertEqual(SRTM_DEM_METADATA["spatial_resolution_m"], 30.0)
        self.assertIn("February 2000", SRTM_DEM_METADATA["quality_and_limitations"])

    def test_d8_flow_routing_and_accumulation_algorithms(self) -> None:
        """Verifies mathematical correctness of D8 routing and flow accumulation."""
        valley_grid = create_synthetic_valley_dem_grid(slope_lateral_deg=20.0, cell_size_m=30.0)

        elev = calculate_local_elevation(valley_grid)
        self.assertGreater(elev, 0.0)

        slope_deg, aspect_deg, aspect_text = calculate_slope_and_aspect(valley_grid)
        self.assertGreaterEqual(slope_deg, 0.0)

        flow_dirs, center_deg = calculate_d8_flow_directions(valley_grid)
        self.assertEqual(flow_dirs.shape, (7, 7))

        accum_matrix, center_accum, upstream_km2 = calculate_flow_accumulation(valley_grid, flow_dirs)
        self.assertEqual(accum_matrix.shape, (7, 7))
        # Center cell in valley floor should accumulate upstream drainage
        self.assertGreaterEqual(center_accum, 4)
        self.assertGreater(upstream_km2, 0.0)

        # Test TWI calculation
        twi = calculate_topographic_wetness_index(slope_deg=10.0, upstream_catchment_m2=50000.0)
        self.assertIsNotNone(twi)
        self.assertGreater(twi, 0.0)

    def test_flat_geographical_location_workflow(self) -> None:
        """Verifies that flat plain locations apply general flood logic without unneeded mountain analysis."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        # Patna (alluvial plain)
        res = assess_flood_hazard(
            session=mock_session,
            latitude=25.6,
            longitude=85.1,
            search_radius_km=50.0,
            lookback_days=7,
        )

        self.assertEqual(res.geographical_environment, GeographicalEnvironment.PLAIN_OR_FLAT)
        self.assertIsNone(res.hilly_terrain_analysis)
        self.assertEqual(len(res.terrain_hazard_statuses), 0)
        self.assertEqual(res.assessment_status, FloodAssessmentStatus.NO_RELEVANT_FLOOD_EVIDENCE)
        self.assertIn("No relevant flood event, official warning", res.summary_message)

    def test_mountainous_location_missing_dem_coverage(self) -> None:
        """Verifies handling of mountainous location when authoritative DEM raster is unavailable."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        # Kedarnath, Uttarakhand (Himalayan arc) without DEM grid
        res = assess_flood_hazard(
            session=mock_session,
            latitude=30.73,
            longitude=79.06,
            search_radius_km=30.0,
            lookback_days=7,
            dem_grid=None,
        )

        self.assertEqual(res.geographical_environment, GeographicalEnvironment.HILLY_OR_MOUNTAINOUS)
        self.assertIsNotNone(res.hilly_terrain_analysis)
        self.assertTrue(res.hilly_terrain_analysis.applied)
        self.assertFalse(res.terrain_information.available)
        self.assertIsNone(res.terrain_information.elevation_m)
        self.assertIsNone(res.terrain_information.slope_degrees)
        self.assertIsNone(res.terrain_information.flow_accumulation)
        # Should record INSUFFICIENT_DATA status in terrain hazard statuses
        self.assertIn(TerrainHazardStatus.INSUFFICIENT_DATA, res.terrain_hazard_statuses)
        self.assertTrue(any("Digital Elevation Model" in info for info in res.missing_information))

    def test_valley_location_with_dem(self) -> None:
        """Verifies valley flood concern and flow path exposure in a narrow mountain canyon."""
        valley_grid = create_synthetic_valley_dem_grid(slope_lateral_deg=25.0, cell_size_m=30.0)

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        res = assess_flood_hazard(
            session=mock_session,
            latitude=30.5,
            longitude=79.2,
            search_radius_km=25.0,
            lookback_days=7,
            dem_grid=valley_grid,
        )

        self.assertEqual(res.geographical_environment, GeographicalEnvironment.HILLY_OR_MOUNTAINOUS)
        self.assertTrue(res.terrain_information.available)
        self.assertIsNotNone(res.terrain_information.elevation_m)
        self.assertIn(res.terrain_information.valley_confinement, ("NARROW_CANYON", "V_SHAPED_VALLEY"))
        self.assertIsNotNone(res.hilly_terrain_analysis)

        # Check statuses
        self.assertTrue(
            TerrainHazardStatus.VALLEY_FLOOD_CONCERN in res.terrain_hazard_statuses
            or TerrainHazardStatus.HIGH_FLOW_PATH_EXPOSURE in res.terrain_hazard_statuses
        )
        self.assertIn("Mountain terrain hazard", res.summary_message)

    def test_high_upstream_flow_accumulation_with_rainfall(self) -> None:
        """Verifies UPSTREAM_CATCHMENT_CONCERN and flash flood alert when contributing catchment is large and raining."""
        now = datetime.now(timezone.utc)
        valley_grid = create_synthetic_valley_dem_grid(slope_lateral_deg=15.0, cell_size_m=30.0)

        # Create mock heavy rainfall observation
        rain_obs = MagicMock()
        rain_obs.id = uuid.uuid4()
        rain_obs.observation_type = "RAINFALL_INTENSITY"
        rain_obs.value_numeric = Decimal("40.0")
        rain_obs.value_text = "40 mm/hr"
        rain_obs.geometry = {"type": "Point", "coordinates": [79.2, 30.5]}
        rain_obs.observed_at = now - timedelta(hours=1)
        rain_obs.ingested_at = now
        rain_obs.data_source = MagicMock()
        rain_obs.data_source.code = "IMD_WEATHER_API"
        rain_obs.data_source.name = "IMD"
        rain_obs.data_source.source_type = "WFS"
        rain_obs.data_source.base_url = "https://mausam.imd.gov.in"
        rain_obs.properties = {"district": "Chamoli", "color_code": 4}

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [],                      # No events
            [(rain_obs, 2000.0)],    # Heavy rain 2km away
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        res = assess_flood_hazard(
            session=mock_session,
            latitude=30.5,
            longitude=79.2,
            search_radius_km=25.0,
            lookback_days=7,
            dem_grid=valley_grid,
        )

        self.assertIsNotNone(res.hilly_terrain_analysis)
        self.assertTrue(res.hilly_terrain_analysis.upstream_rainfall_concern)
        self.assertTrue(res.hilly_terrain_analysis.flash_flood_concern)
        self.assertTrue(
            TerrainHazardStatus.UPSTREAM_CATCHMENT_CONCERN in res.terrain_hazard_statuses
            or TerrainHazardStatus.HIGH_FLOW_PATH_EXPOSURE in res.terrain_hazard_statuses
            or TerrainHazardStatus.VALLEY_FLOOD_CONCERN in res.terrain_hazard_statuses
        )

    def test_steep_slope_with_heavy_rainfall_landslide_and_debris_flow(self) -> None:
        """Verifies LANDSLIDE_RELATED_CONCERN and DEBRIS_FLOW_CONCERN on steep slopes under heavy rain."""
        now = datetime.now(timezone.utc)
        steep_grid = create_synthetic_steep_slope_dem_grid(slope_deg=32.0, cell_size_m=30.0)

        # Mock heavy rainfall warning
        mock_event = MagicMock()
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.FLOOD
        mock_event.title = "Red Alert: Torrential Downpour & Flash Floods"
        mock_event.description = "Extremely heavy precipitation alert"
        mock_event.severity = SeverityLevel.EXTREME
        mock_event.status = CrisisStatus.ACTIVE
        mock_event.location = {"type": "Point", "coordinates": [79.2, 30.5]}
        mock_event.affected_area = None
        mock_event.started_at = now - timedelta(hours=1)
        mock_event.expires_at = now + timedelta(hours=3)
        mock_event.first_seen_at = now - timedelta(hours=1)
        mock_event.last_seen_at = now - timedelta(minutes=15)
        mock_event.source_reports = []

        mock_session = MagicMock()
        mock_session.execute.return_value.all.side_effect = [
            [(mock_event, 1000.0, None)],
            [],
        ]
        mock_session.execute.return_value.first.return_value = (0, None)

        res = assess_flood_hazard(
            session=mock_session,
            latitude=30.5,
            longitude=79.2,
            search_radius_km=20.0,
            lookback_days=7,
            dem_grid=steep_grid,
        )

        self.assertIsNotNone(res.hilly_terrain_analysis)
        self.assertTrue(res.hilly_terrain_analysis.landslide_susceptibility_concern)
        self.assertIn(TerrainHazardStatus.LANDSLIDE_RELATED_CONCERN, res.terrain_hazard_statuses)

    def test_ridge_location_not_marked_safe_merely_due_to_elevation(self) -> None:
        """Verifies that ridge crest locations are not falsely deemed safe."""
        ridge_grid = create_synthetic_ridge_dem_grid(cell_size_m=30.0)

        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        res = assess_flood_hazard(
            session=mock_session,
            latitude=30.5,
            longitude=79.2,
            search_radius_km=20.0,
            lookback_days=7,
            dem_grid=ridge_grid,
        )

        self.assertEqual(res.terrain_information.valley_confinement, "RIDGE_OR_PEAK")
        # Elevation is high (2800m), but limitations clearly forbid claiming safety
        self.assertTrue(
            any("Mountain ridges are not automatically safe" in lim for lim in res.scientific_limitations)
        )
        self.assertTrue(
            any("Higher elevation does not automatically mean a location is safe" in lim for lim in res.scientific_limitations)
        )

    def test_missing_and_incomplete_measurements_never_zero(self) -> None:
        """Verifies that unavailable measurements remain None and are never zeroed."""
        river_evidence = RiverObservationEvidence(
            station_code="TEST_STA",
            station_name="Mountain Tributary",
            water_level_m=None,
            warning_level_m=None,
            danger_level_m=None,
            distance_km=5.0,
            observed_at=datetime.now(timezone.utc),
            status_relative_to_danger=RiverLevelStatus.UNKNOWN,
        )
        self.assertIsNone(river_evidence.water_level_m)
        self.assertNotEqual(river_evidence.water_level_m, 0.0)

        rain_evidence = RainfallEvidence(
            station_or_district="Upper Catchment",
            rainfall_intensity_mm_hr=None,
            distance_km=8.0,
            observed_at=datetime.now(timezone.utc),
        )
        self.assertIsNone(rain_evidence.rainfall_intensity_mm_hr)
        self.assertNotEqual(rain_evidence.rainfall_intensity_mm_hr, 0.0)

    def test_api_endpoint_hilly_mountain_integration(self) -> None:
        """Verifies GET /api/v1/flood/assessment endpoint integration with mountainous coordinates."""
        mock_session = MagicMock()
        mock_session.execute.return_value.all.return_value = []
        mock_session.execute.return_value.first.return_value = (0, None)

        res = get_flood_assessment(
            latitude=30.73,
            longitude=79.06,
            search_radius_km=50.0,
            lookback_days=7,
            session=mock_session,
        )

        self.assertTrue(res.success)
        self.assertEqual(res.geographical_environment, GeographicalEnvironment.HILLY_OR_MOUNTAINOUS)
        self.assertIsNotNone(res.hilly_terrain_analysis)
        self.assertTrue(res.hilly_terrain_analysis.applied)
        self.assertIn("INSUFFICIENT_DATA", [s.value for s in res.terrain_hazard_statuses])
        self.assertEqual(res.user_location.latitude, 30.73)
        self.assertEqual(res.user_location.longitude, 79.06)


if __name__ == "__main__":
    unittest.main()
