from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.routes import calculate_route
from app.main import app
from app.models.core import CrisisEvent, DataSource
from app.models.enums import (
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    RiskLevel,
    RouteSafetyStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import (
    EvacuationRoute,
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
)
from app.schemas.route import (
    CalculateRouteRequest,
    CalculateRouteResponse,
    RouteClassification,
)
from app.services.evacuation_routing import (
    ROUTING_LIMITATIONS,
    ROUTING_PROVIDER_NAME,
    SAFETY_DISCLAIMER,
    analyze_route_hazards,
    calculate_evacuation_routes,
    call_osrm_driving_route,
    check_destination_eligibility,
    compare_and_rank_routes,
)


class EvacuationRoutingTests(unittest.TestCase):
    def test_route_api_endpoints_registered(self) -> None:
        """Verifies OpenAPI schema registers POST /api/v1/routes/calculate with correct schema constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})

        self.assertIn("/api/v1/routes/calculate", paths)
        post_op = paths["/api/v1/routes/calculate"]["post"]
        self.assertIsNotNone(post_op)

        # Verify request body schema
        req_content = post_op["requestBody"]["content"]["application/json"]["schema"]
        ref_name = req_content["$ref"].split("/")[-1]
        self.assertEqual(ref_name, "CalculateRouteRequest")

        schemas = openapi_schema["components"]["schemas"]
        req_schema = schemas["CalculateRouteRequest"]
        self.assertIn("origin_latitude", req_schema["properties"])
        self.assertIn("origin_longitude", req_schema["properties"])
        self.assertIn("location_assessment_id", req_schema["properties"])

    def test_destination_eligibility_enforced(self) -> None:
        """Verifies that hard-excluded or not-recommended destinations are rejected before route calculation."""
        # 1. Hard-excluded assessment
        loc = SafeLocation(id=uuid.uuid4(), name="Submerged Park", category="PARK")
        excluded_assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "FLOOD",
                "hard_exclusion_status": True,
                "hard_exclusion_reasons": ["Location submerged under active floodwaters"],
                "suitability_classification": "EXCLUDED",
            },
        )

        is_eligible, reason, dt = check_destination_eligibility(excluded_assessment)
        self.assertFalse(is_eligible)
        self.assertIn("Ineligible destination", reason or "")
        self.assertIn("active disaster hard exclusion", reason or "")
        self.assertEqual(dt, CrisisType.FLOOD)

        # 2. Not Recommended assessment
        not_rec_assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "EARTHQUAKE",
                "hard_exclusion_status": False,
                "suitability_classification": "NOT_RECOMMENDED",
            },
        )
        is_eligible_nr, reason_nr, dt_nr = check_destination_eligibility(not_rec_assessment)
        self.assertFalse(is_eligible_nr)
        self.assertIn("NOT_RECOMMENDED", reason_nr or "")
        self.assertEqual(dt_nr, CrisisType.EARTHQUAKE)

        # 3. Eligible assessment (SUITABILITY_SUPPORTED_BY_EVIDENCE)
        good_assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "CYCLONE",
                "hard_exclusion_status": False,
                "suitability_classification": "SUITABILITY_SUPPORTED_BY_EVIDENCE",
            },
        )
        is_eligible_g, reason_g, dt_g = check_destination_eligibility(good_assessment)
        self.assertTrue(is_eligible_g)
        self.assertIsNone(reason_g)
        self.assertEqual(dt_g, CrisisType.CYCLONE)

    def test_flood_active_inundation_route_hard_exclusion(self) -> None:
        """Verifies that route traversing or directly bordering active flood inundation extent is marked ROUTE_EXCLUDED."""
        # Simulated active flood inundation event at (26.1800, 91.7500)
        flood_event = MagicMock(spec=CrisisEvent)
        flood_event.id = uuid.uuid4()
        flood_event.title = "Brahmaputra Basin Active Inundation"
        flood_event.description = "Deep inundation over roadways and fields"
        flood_event.crisis_type = CrisisType.FLOOD
        flood_event.status = CrisisStatus.ACTIVE
        flood_event.severity = MagicMock(value="SEVERE")
        flood_event.started_at = datetime.now(timezone.utc)
        flood_event.location = MagicMock()

        # Route coordinates passing directly across (26.1801, 91.7501)
        route_coords = [
            [91.7450, 26.1800],
            [91.7501, 26.1801],
            [91.7550, 26.1800],
        ]

        with patch("app.services.evacuation_routing.safe_postgis_to_geojson") as mock_geo:
            mock_geo.return_value = {"type": "Point", "coordinates": [91.7500, 26.1800]}
            hazards, warnings, hard_excluded, hard_reasons = analyze_route_hazards(
                route_coords=route_coords,
                disaster_type=CrisisType.FLOOD,
                active_events=[flood_event],
            )

        self.assertTrue(hard_excluded)
        self.assertTrue(any("active flood inundation" in r.lower() for r in hard_reasons))
        self.assertTrue(any(h.is_hard_exclusion for h in hazards))

    def test_cyclone_coastal_surge_route_hard_exclusion(self) -> None:
        """Verifies that route entering coastal surge zone (<= 5 km from sea) during active cyclone is hard excluded."""
        active_cyclone = MagicMock(spec=CrisisEvent)
        active_cyclone.id = uuid.uuid4()
        active_cyclone.title = "Severe Cyclonic Storm Warning"
        active_cyclone.crisis_type = CrisisType.CYCLONE
        active_cyclone.status = CrisisStatus.ACTIVE
        active_cyclone.severity = MagicMock(value="SEVERE")
        active_cyclone.started_at = datetime.now(timezone.utc)
        active_cyclone.location = MagicMock()

        # Puri beach coastal route (lat: 19.802, lon: 85.705; right on coastal line chain)
        coastal_route_coords = [
            [85.700, 19.800],
            [85.705, 19.802],
            [85.710, 19.805],
        ]

        with patch("app.services.evacuation_routing.safe_postgis_to_geojson") as mock_geo:
            mock_geo.return_value = {"type": "Point", "coordinates": [85.825, 19.798]}
            hazards, warnings, hard_excluded, hard_reasons = analyze_route_hazards(
                route_coords=coastal_route_coords,
                disaster_type=CrisisType.CYCLONE,
                active_events=[active_cyclone],
            )

        self.assertTrue(hard_excluded)
        self.assertTrue(any("storm surge" in r.lower() for r in hard_reasons))
        self.assertTrue(any(h.hazard_type == "STORM_SURGE_ZONE" for h in hazards))

    def test_mountainous_terrain_hazards_identified(self) -> None:
        """Verifies that route traversing mountainous terrain flags slope failure / rockfall in earthquake and valley flash runoff in flood."""
        # Dehradun to Mussoorie mountain route (lat: 30.45, lon: 78.07)
        mountain_coords = [
            [78.070, 30.440],
            [78.072, 30.450],
            [78.075, 30.460],
        ]

        # 1. Earthquake in mountain
        haz_eq, warn_eq, exc_eq, _ = analyze_route_hazards(
            route_coords=mountain_coords,
            disaster_type=CrisisType.EARTHQUAKE,
            active_events=[],
        )
        self.assertFalse(exc_eq)
        self.assertTrue(any("slope failure" in w.lower() or "rockfall" in w.lower() for w in warn_eq))
        self.assertTrue(any(h.hazard_type == "SEISMIC_SLOPE_HAZARD" for h in haz_eq))

        # 2. Flood in mountain
        haz_fl, warn_fl, exc_fl, _ = analyze_route_hazards(
            route_coords=mountain_coords,
            disaster_type=CrisisType.FLOOD,
            active_events=[],
        )
        self.assertFalse(exc_fl)
        self.assertTrue(any("flash flood" in w.lower() or "valley" in w.lower() for w in warn_fl))
        self.assertTrue(any(h.hazard_type == "FLASH_FLOOD_CORRIDOR" for h in haz_fl))

    def test_alternative_route_comparison_and_ranking(self) -> None:
        """Verifies that an alternative route with fewer hazard exposures is preferred over a shorter hazardous route."""
        # Candidate 1: Shorter (3.0 km, 300s) but passes through an active flood inundation extent
        short_hazardous_route = {
            "distance": 3000.0,
            "duration": 300.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [91.7450, 26.1800],
                    [91.7501, 26.1801],  # Close to flood event
                    [91.7550, 26.1800],
                ],
            },
        }

        # Candidate 2: Longer (5.5 km, 550s) bypassing the flood area entirely
        long_safe_route = {
            "distance": 5500.0,
            "duration": 550.0,
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [91.7450, 26.1800],
                    [91.7450, 26.2100],  # ~3 km north, away from flood
                    [91.7550, 26.2100],
                    [91.7550, 26.1800],
                ],
            },
        }

        flood_event = MagicMock(spec=CrisisEvent)
        flood_event.id = uuid.uuid4()
        flood_event.title = "Active Inundation Zone"
        flood_event.description = "Submerged road"
        flood_event.crisis_type = CrisisType.FLOOD
        flood_event.status = CrisisStatus.ACTIVE
        flood_event.severity = MagicMock(value="HIGH")
        flood_event.started_at = datetime.now(timezone.utc)
        flood_event.location = MagicMock()

        with patch("app.services.evacuation_routing.safe_postgis_to_geojson") as mock_geo:
            mock_geo.return_value = {"type": "Point", "coordinates": [91.7500, 26.1800]}
            ranked = compare_and_rank_routes(
                candidate_routes=[short_hazardous_route, long_safe_route],
                disaster_type=CrisisType.FLOOD,
                active_events=[flood_event],
            )

        self.assertEqual(len(ranked), 2)
        # Verify that the safe route was promoted to Rank 1 (Primary), despite being 2.5 km longer!
        primary = ranked[0]
        self.assertFalse(primary.is_hard_excluded)
        self.assertEqual(primary.distance_m, 5500.0)

        # Verify that the shorter route was demoted due to hard exclusion
        secondary = ranked[1]
        self.assertTrue(secondary.is_hard_excluded)
        self.assertEqual(secondary.route_status, RouteClassification.ROUTE_EXCLUDED)
        self.assertEqual(secondary.distance_m, 3000.0)

    def test_routing_service_unavailable_handled_gracefully(self) -> None:
        """Verifies that OSRM failure or timeout returns ROUTING_SERVICE_UNAVAILABLE without inventing straight lines."""
        mock_session = MagicMock()
        loc = SafeLocation(id=uuid.uuid4(), name="Cyclone Shelter Beta", category="OFFICIAL_CYCLONE_SHELTER")
        loc.location = MagicMock()

        assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "CYCLONE",
                "hard_exclusion_status": False,
                "suitability_classification": "SUITABILITY_SUPPORTED_BY_EVIDENCE",
            },
        )
        mock_session.scalars.return_value.first.return_value = assessment
        mock_session.scalars.return_value.all.return_value = []

        # Mock OSRM returning an error (e.g. connection refused or timeout)
        def mock_osrm_fail(*args: Any, **kwargs: Any) -> tuple[list[dict], str | None]:
            return [], "Routing service unreachable: Connection timed out"

        with patch("app.services.evacuation_routing.extract_location_coordinates") as mock_extract:
            mock_extract.return_value = (20.4285, 86.5812)
            resp = calculate_evacuation_routes(
                session=mock_session,
                origin_latitude=20.4000,
                origin_longitude=86.5500,
                location_assessment_id=assessment.id,
                osrm_client_fn=mock_osrm_fail,
            )

        self.assertFalse(resp.success)
        self.assertEqual(resp.route_status, RouteClassification.ROUTING_SERVICE_UNAVAILABLE)
        self.assertIsNone(resp.primary_route)
        self.assertIn("Routing service could not provide a route", resp.comparison_summary or "")

    def test_all_routes_excluded_returns_no_safe_route_found(self) -> None:
        """Verifies that when all available routes intersect exclusion zones, NO_SAFE_ROUTE_FOUND is returned."""
        mock_session = MagicMock()
        loc = SafeLocation(id=uuid.uuid4(), name="Flood Safe High Ground", category="COMMUNITY_CENTER")
        loc.location = MagicMock()

        assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "FLOOD",
                "hard_exclusion_status": False,
                "suitability_classification": "SUITABILITY_SUPPORTED_BY_EVIDENCE",
            },
        )
        mock_session.scalars.return_value.first.return_value = assessment

        # Flood event blocking the only road
        flood_event = MagicMock(spec=CrisisEvent)
        flood_event.id = uuid.uuid4()
        flood_event.title = "Submerged Bridge Inundation"
        flood_event.crisis_type = CrisisType.FLOOD
        flood_event.status = CrisisStatus.ACTIVE
        flood_event.severity = MagicMock(value="SEVERE")
        flood_event.started_at = datetime.now(timezone.utc)
        flood_event.location = MagicMock()

        mock_session.scalars.return_value.all.return_value = [flood_event]

        # Single candidate route that intersects the submerged bridge
        single_route = [
            {
                "distance": 4000.0,
                "duration": 400.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[91.7500, 26.1800], [91.7502, 26.1802]],
                },
            }
        ]

        def mock_osrm_single(*args: Any, **kwargs: Any) -> tuple[list[dict], str | None]:
            return single_route, None

        with patch("app.services.evacuation_routing.extract_location_coordinates") as mock_extract:
            mock_extract.return_value = (26.1850, 91.7550)
            with patch("app.services.evacuation_routing.safe_postgis_to_geojson") as mock_geo:
                mock_geo.return_value = {"type": "Point", "coordinates": [91.7501, 26.1801]}
                with patch("app.services.evacuation_routing.geojson_to_postgis") as mock_g2p:
                    mock_g2p.return_value = MagicMock()
                    resp = calculate_evacuation_routes(
                        session=mock_session,
                        origin_latitude=26.1750,
                        origin_longitude=91.7450,
                        location_assessment_id=assessment.id,
                        osrm_client_fn=mock_osrm_single,
                    )

        self.assertEqual(resp.route_status, RouteClassification.NO_SAFE_ROUTE_FOUND)
        self.assertTrue(resp.primary_route.is_hard_excluded)
        self.assertIn("No safe evacuation route found", resp.comparison_summary or "")

    def test_database_persistence_of_evacuation_route(self) -> None:
        """Verifies that calculate_evacuation_routes persists EvacuationRoute records with PostGIS geometry and hazard intersections."""
        mock_session = MagicMock()
        loc = SafeLocation(id=uuid.uuid4(), name="Verified Community Shelter", category="COMMUNITY_CENTER")
        loc.location = MagicMock()

        assessment = LocationAssessment(
            id=uuid.uuid4(),
            safe_location=loc,
            factors={
                "disaster_type": "EARTHQUAKE",
                "hard_exclusion_status": False,
                "suitability_classification": "SUITABILITY_SUPPORTED_BY_EVIDENCE",
            },
        )
        mock_session.scalars.return_value.first.return_value = assessment
        mock_session.scalars.return_value.all.return_value = []

        valid_route = [
            {
                "distance": 6200.0,
                "duration": 520.0,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[85.1376, 25.5941], [85.1450, 25.6000]],
                },
            }
        ]

        def mock_osrm_valid(*args: Any, **kwargs: Any) -> tuple[list[dict], str | None]:
            return valid_route, None

        with patch("app.services.evacuation_routing.extract_location_coordinates") as mock_extract:
            mock_extract.return_value = (25.6000, 85.1450)
            with patch("app.services.evacuation_routing.geojson_to_postgis") as mock_g2p:
                mock_g2p.return_value = MagicMock()
                resp = calculate_evacuation_routes(
                    session=mock_session,
                    origin_latitude=25.5941,
                    origin_longitude=85.1376,
                    location_assessment_id=assessment.id,
                    osrm_client_fn=mock_osrm_valid,
                )

        self.assertTrue(resp.success)
        self.assertEqual(resp.route_status, RouteClassification.ROUTE_AVAILABLE)
        self.assertIsNotNone(resp.primary_route)

        # Verify EvacuationRoute record added to DB session
        added_objs = [call[0][0] for call in mock_session.add.call_args_list]
        evac_routes = [o for o in added_objs if isinstance(o, EvacuationRoute)]
        self.assertEqual(len(evac_routes), 1)
        self.assertEqual(evac_routes[0].location_assessment_id, assessment.id)
        self.assertEqual(evac_routes[0].route_safety_status, RouteSafetyStatus.SAFE)
        self.assertEqual(evac_routes[0].routing_provider, ROUTING_PROVIDER_NAME)

        # Verify session commit called
        mock_session.commit.assert_called_once()

    def test_api_endpoint_invokes_routing_engine(self) -> None:
        """Verifies POST /api/v1/routes/calculate delegates cleanly to calculate_evacuation_routes service."""
        mock_session = MagicMock()
        assessment_id = uuid.uuid4()
        req = CalculateRouteRequest(
            origin_latitude=28.6139,
            origin_longitude=77.2090,
            location_assessment_id=assessment_id,
            allow_alternatives=True,
        )

        with patch("app.api.v1.routes.calculate_evacuation_routes") as mock_service:
            mock_resp = CalculateRouteResponse(
                success=True,
                route_status=RouteClassification.ROUTE_AVAILABLE,
                origin={"latitude": 28.6139, "longitude": 77.2090},
                destination_id=uuid.uuid4(),
                disaster_type=CrisisType.EARTHQUAKE,
                total_routes_analyzed=1,
                routing_provider=ROUTING_PROVIDER_NAME,
                routing_limitations=ROUTING_LIMITATIONS,
                safety_disclaimer=SAFETY_DISCLAIMER,
            )
            mock_service.return_value = mock_resp

            res = calculate_route(request=req, session=mock_session)
            self.assertEqual(res.route_status, RouteClassification.ROUTE_AVAILABLE)
            mock_service.assert_called_once_with(
                session=mock_session,
                origin_latitude=28.6139,
                origin_longitude=77.2090,
                location_assessment_id=assessment_id,
                allow_alternatives=True,
            )


if __name__ == "__main__":
    unittest.main()
