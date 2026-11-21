from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.recommendations import get_unified_recommendations
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
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
)
from app.schemas.disaster_search import UserLocation
from app.schemas.recommendation import (
    LocationSuitabilityAssessmentItem,
    SuitabilityClassification,
)
from app.schemas.route import (
    CalculateRouteResponse,
    GeoJSONLineString,
    RouteClassification,
    RouteDetailItem,
)
from app.schemas.unified_recommendation import (
    UnifiedRecommendationRequest,
    UnifiedRecommendationResponse,
    UnifiedRecommendationStatus,
)
from app.services.unified_recommendation import (
    UNIFIED_SAFETY_DISCLAIMER,
    generate_unified_recommendations,
)


class UnifiedRecommendationTests(unittest.TestCase):
    def test_unified_recommendation_api_registration(self) -> None:
        """Verifies OpenAPI schema registers POST /api/v1/recommendations/unified."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})

        self.assertIn("/api/v1/recommendations/unified", paths)
        post_op = paths["/api/v1/recommendations/unified"]["post"]
        self.assertIsNotNone(post_op)

        req_content = post_op["requestBody"]["content"]["application/json"]["schema"]
        ref_name = req_content["$ref"].split("/")[-1]
        self.assertEqual(ref_name, "UnifiedRecommendationRequest")

        schemas = openapi_schema["components"]["schemas"]
        self.assertIn("UnifiedRecommendationRequest", schemas)
        self.assertIn("UnifiedRecommendationResponse", schemas)
        self.assertIn("UnifiedRecommendationStatus", schemas)

    def test_invalid_crisis_event_id_handling(self) -> None:
        """Verifies DISASTER_CONTEXT_UNAVAILABLE is returned when crisis_event_id does not exist or type mismatches."""
        mock_session = MagicMock()
        # Non-existent event
        mock_session.scalars.return_value.first.return_value = None

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            disaster_type=CrisisType.EARTHQUAKE,
            crisis_event_id=uuid.uuid4(),
        )

        self.assertFalse(resp.success)
        self.assertEqual(resp.status, UnifiedRecommendationStatus.DISASTER_CONTEXT_UNAVAILABLE)
        self.assertIn("could not be verified", resp.hazard_assessment.summary)
        self.assertIn(UNIFIED_SAFETY_DISCLAIMER, resp.safety_disclaimer)

        # Mismatched crisis event type (Event is CYCLONE, but requested EARTHQUAKE)
        mock_event = MagicMock(spec=CrisisEvent)
        mock_event.id = uuid.uuid4()
        mock_event.crisis_type = CrisisType.CYCLONE
        mock_session.scalars.return_value.first.return_value = mock_event

        resp_mismatch = generate_unified_recommendations(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            disaster_type=CrisisType.EARTHQUAKE,
            crisis_event_id=mock_event.id,
        )
        self.assertFalse(resp_mismatch.success)
        self.assertEqual(resp_mismatch.status, UnifiedRecommendationStatus.DISASTER_CONTEXT_UNAVAILABLE)

    @patch("app.services.unified_recommendation.assess_earthquake_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    @patch("app.services.unified_recommendation.calculate_evacuation_routes")
    def test_earthquake_unified_recommendation_workflow(
        self,
        mock_calc_routes: MagicMock,
        mock_assess_suitability: MagicMock,
        mock_assess_earthquake: MagicMock,
    ) -> None:
        """Verifies end-to-end earthquake recommendation workflow, evidence-based ranking, and response schema."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        # Mock earthquake assessment
        mock_eq_resp = MagicMock()
        mock_eq_resp.status = MagicMock(value="RECENT_EARTHQUAKES_FOUND")
        mock_eq_resp.assessment_summary = "M5.2 earthquake reported 45 km north; moderate shaking."
        mock_eq_resp.data_freshness = "FRESH"
        mock_eq_resp.earthquakes = [
            MagicMock(magnitude=5.2, magnitude_type="ML", location_name="Kangra", distance_km=45.2, depth_km=15.0)
        ]
        mock_eq_resp.contributing_factors = ["Shallow focal depth"]
        mock_eq_resp.scientific_limitations = ["Earthquakes cannot be predicted in advance."]
        mock_assess_earthquake.return_value = mock_eq_resp

        # Mock safe locations suitability assessment (2 candidates: 1 official verified, 1 unverified)
        loc1_id = uuid.uuid4()
        assess1_id = uuid.uuid4()
        cand1 = LocationSuitabilityAssessmentItem(
            assessment_id=assess1_id,
            location_id=loc1_id,
            name="District Sports Complex",
            category="sports_complex",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            verification_detail="OFFICIALLY_VERIFIED",
            distance_km=3.2,
            disaster_type=CrisisType.EARTHQUAKE,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.92,
            supporting_evidence=["Reinforced concrete framework", "Open space clearance"],
            warnings=[],
            assessed_at=now,
        )

        loc2_id = uuid.uuid4()
        assess2_id = uuid.uuid4()
        cand2 = LocationSuitabilityAssessmentItem(
            assessment_id=assess2_id,
            location_id=loc2_id,
            name="Community Hall B",
            category="community_hall",
            official_designation=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            verification_detail="UNVERIFIED",
            distance_km=2.1,
            disaster_type=CrisisType.EARTHQUAKE,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.85,
            supporting_evidence=["Single-storey building"],
            warnings=[],
            assessed_at=now,
        )

        mock_suit_resp = MagicMock()
        mock_suit_resp.assessed_locations = [cand1, cand2]
        mock_assess_suitability.return_value = mock_suit_resp

        # Mock route calculations (both receive ROUTE_AVAILABLE)
        route_item1 = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[77.20, 28.61], [77.22, 28.62]]),
            distance_m=3200.0,
            distance_km=3.2,
            estimated_time_seconds=360,
            estimated_time_minutes=6.0,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            hazard_exposure_level="LOW",
            hazard_intersections_count=0,
            hazard_intersections=[],
            warnings=[],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        route_resp1 = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            origin={"latitude": 28.6139, "longitude": 77.2090},
            destination_id=loc1_id,
            disaster_type=CrisisType.EARTHQUAKE,
            primary_route=route_item1,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        route_item2 = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[77.20, 28.61], [77.21, 28.615]]),
            distance_m=2100.0,
            distance_km=2.1,
            estimated_time_seconds=240,
            estimated_time_minutes=4.0,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            hazard_exposure_level="LOW",
            hazard_intersections_count=0,
            hazard_intersections=[],
            warnings=[],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        route_resp2 = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            origin={"latitude": 28.6139, "longitude": 77.2090},
            destination_id=loc2_id,
            disaster_type=CrisisType.EARTHQUAKE,
            primary_route=route_item2,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        mock_calc_routes.side_effect = [route_resp1, route_resp2]

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=28.6139,
            longitude=77.2090,
            disaster_type=CrisisType.EARTHQUAKE,
            search_radius_km=10.0,
        )

        self.assertTrue(resp.success)
        self.assertEqual(resp.status, UnifiedRecommendationStatus.RECOMMENDATIONS_AVAILABLE)
        self.assertEqual(len(resp.recommendations), 2)

        # Candidate 1 (District Sports Complex) must be Rank 1 because it is OFFICIALLY VERIFIED
        self.assertEqual(resp.recommendations[0].name, "District Sports Complex")
        self.assertEqual(resp.recommendations[0].official_designation, DesignationStatus.OFFICIAL)
        self.assertIn("Officially designated/verified", resp.recommendations[0].ranking_rationale)

        # Candidate 2 is Rank 2
        self.assertEqual(resp.recommendations[1].name, "Community Hall B")
        self.assertIn("Unverified", resp.recommendations[1].ranking_rationale)

        self.assertEqual(resp.hazard_assessment.disaster_type, CrisisType.EARTHQUAKE)
        self.assertIn(UNIFIED_SAFETY_DISCLAIMER, resp.safety_disclaimer)

    @patch("app.services.unified_recommendation.assess_flood_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    @patch("app.services.unified_recommendation.calculate_evacuation_routes")
    def test_flood_hard_exclusion_handling(
        self,
        mock_calc_routes: MagicMock,
        mock_assess_suitability: MagicMock,
        mock_assess_flood: MagicMock,
    ) -> None:
        """Verifies submerged/hard-excluded candidates are filtered out from top recommendations."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        # Mock flood assessment
        mock_fl_resp = MagicMock()
        mock_fl_resp.status = MagicMock(value="ACTIVE_FLOOD_OBSERVED")
        mock_fl_resp.assessment_summary = "River water levels above danger threshold; active inundation."
        mock_fl_resp.observed_flooding = [MagicMock(distance_km=1.2, inundation_depth_meters=1.5)]
        mock_fl_resp.official_warnings = [MagicMock(headline="Severe Flood Warning", warning_level="RED", source_name="CWC")]
        mock_fl_resp.river_observations = []
        mock_fl_resp.contributing_factors = ["Continuous precipitation"]
        mock_fl_resp.scientific_limitations = ["Hydraulic backflow can cause unpredicted flooding."]
        mock_assess_flood.return_value = mock_fl_resp

        # 3 Candidates: 1 submerged/excluded, 1 evidence-backed, 1 conditional
        cand_excluded = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Low-Lying School Ground",
            category="school",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            distance_km=1.1,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.EXCLUDED,
            suitability_score=0.0,
            hard_exclusion_status=True,
            hard_exclusion_reasons=["Directly inundated by active floodwaters (1.5m depth)."],
            assessed_at=now,
        )

        cand_evidence = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Hilltop Community Center",
            category="community_hall",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            distance_km=4.5,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.88,
            supporting_evidence=["Elevated ridge", "25m above base river bed"],
            assessed_at=now,
        )

        cand_conditional = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Multi-Purpose Shelter C",
            category="shelter",
            official_designation=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            distance_km=2.8,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.CONDITIONALLY_SUITABLE,
            suitability_score=0.55,
            supporting_evidence=["Two-storey concrete facility"],
            warnings=["Ground floor prone to pluvial waterlogging"],
            assessed_at=now,
        )

        mock_suit_resp = MagicMock()
        mock_suit_resp.assessed_locations = [cand_excluded, cand_evidence, cand_conditional]
        mock_assess_suitability.return_value = mock_suit_resp

        # Route mocks for the two non-excluded candidates
        route_item = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[91.74, 26.18], [91.76, 26.20]]),
            distance_m=4500.0,
            distance_km=4.5,
            estimated_time_seconds=450,
            estimated_time_minutes=7.5,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            hazard_exposure_level="LOW",
            hazard_intersections_count=0,
            hazard_intersections=[],
            warnings=[],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        mock_calc_routes.return_value = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            origin={"latitude": 26.18, "longitude": 91.74},
            destination_id=cand_evidence.location_id,
            disaster_type=CrisisType.FLOOD,
            primary_route=route_item,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=26.18,
            longitude=91.74,
            disaster_type=CrisisType.FLOOD,
        )

        self.assertTrue(resp.success)
        self.assertEqual(resp.excluded_destinations_count, 1)
        self.assertEqual(resp.total_suitable_destinations, 2)
        # Verify the excluded candidate was NOT included in the recommendations
        rec_names = [r.name for r in resp.recommendations]
        self.assertNotIn("Low-Lying School Ground", rec_names)
        self.assertIn("Hilltop Community Center", rec_names)

        # Hilltop Community Center (Evidence-backed) must outrank Multi-Purpose Shelter C (Conditional)
        self.assertEqual(resp.recommendations[0].name, "Hilltop Community Center")

    @patch("app.services.unified_recommendation.assess_cyclone_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    @patch("app.services.unified_recommendation.calculate_evacuation_routes")
    def test_cyclone_route_hazard_exposure_ranking(
        self,
        mock_calc_routes: MagicMock,
        mock_assess_suitability: MagicMock,
        mock_assess_cyclone: MagicMock,
    ) -> None:
        """Verifies safer route with 0 hazard intersections outranks closer destination with route hazard warnings."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        # Mock cyclone assessment
        mock_cy_resp = MagicMock()
        mock_cy_resp.status = MagicMock(value="ACTIVE_CYCLONE_HAZARD")
        mock_cy_resp.assessment_summary = "Very Severe Cyclonic Storm approaching coast."
        mock_cy_resp.data_freshness = "FRESH"
        mock_cy_resp.wind_hazard = MagicMock(intensity_category="VERY_SEVERE_CYCLONIC_STORM", max_sustained_wind_kmph=145.0)
        mock_cy_resp.storm_surge_hazard = MagicMock(storm_surge_meters=3.2)
        mock_cy_resp.coastal_hazard = MagicMock(distance_to_coastline_km=4.5)
        mock_cy_resp.contributing_factors = ["High gale winds", "Inundation risk in low-lying coastal belts"]
        mock_cy_resp.scientific_limitations = ["Track forecast cone expands with lead time."]
        mock_assess_cyclone.return_value = mock_cy_resp

        # 2 Candidate shelters:
        # Candidate A: Closer (3.0 km) but route has hazard warnings (coastal surge intersection)
        # Candidate B: Farther (7.0 km) inland with clear route (0 hazard intersections)
        cand_a = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Coastal Beach Shelter",
            category="shelter",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            distance_km=3.0,
            disaster_type=CrisisType.CYCLONE,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.85,
            supporting_evidence=["Reinforced concrete cyclone shelter"],
            assessed_at=now,
        )

        cand_b = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Inland Elevated College",
            category="college",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            distance_km=7.0,
            disaster_type=CrisisType.CYCLONE,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.85,
            supporting_evidence=["Multi-storey reinforced institution", "12 km from coast"],
            assessed_at=now,
        )

        mock_suit_resp = MagicMock()
        mock_suit_resp.assessed_locations = [cand_a, cand_b]
        mock_assess_suitability.return_value = mock_suit_resp

        # Route for Candidate A: ROUTE_WITH_WARNINGS (1 hazard warning)
        route_item_a = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[85.80, 19.80], [85.82, 19.81]]),
            distance_m=3000.0,
            distance_km=3.0,
            estimated_time_seconds=300,
            estimated_time_minutes=5.0,
            route_status=RouteClassification.ROUTE_WITH_WARNINGS,
            hazard_exposure_level="HIGH",
            hazard_intersections_count=1,
            hazard_intersections=[],
            warnings=["Traverses coastal surge vulnerability zone"],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        route_resp_a = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_WITH_WARNINGS,
            origin={"latitude": 19.80, "longitude": 85.80},
            destination_id=cand_a.location_id,
            disaster_type=CrisisType.CYCLONE,
            primary_route=route_item_a,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        # Route for Candidate B: ROUTE_AVAILABLE (0 hazard intersections)
        route_item_b = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[85.80, 19.80], [85.80, 19.86]]),
            distance_m=7000.0,
            distance_km=7.0,
            estimated_time_seconds=600,
            estimated_time_minutes=10.0,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            hazard_exposure_level="LOW",
            hazard_intersections_count=0,
            hazard_intersections=[],
            warnings=[],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        route_resp_b = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_AVAILABLE,
            origin={"latitude": 19.80, "longitude": 85.80},
            destination_id=cand_b.location_id,
            disaster_type=CrisisType.CYCLONE,
            primary_route=route_item_b,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        mock_calc_routes.side_effect = [route_resp_a, route_resp_b]

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=19.80,
            longitude=85.80,
            disaster_type=CrisisType.CYCLONE,
        )

        self.assertTrue(resp.success)
        self.assertEqual(len(resp.recommendations), 2)

        # Inland Elevated College must be Rank 1 because its road route has ROUTE_AVAILABLE (0 hazard intersections),
        # even though Coastal Beach Shelter is 4 km closer!
        self.assertEqual(resp.recommendations[0].name, "Inland Elevated College")
        self.assertEqual(resp.recommendations[0].route_status, RouteClassification.ROUTE_AVAILABLE)
        self.assertIn("Clear road route (0 hazard intersections", resp.recommendations[0].ranking_rationale)

        # Coastal Beach Shelter is Rank 2
        self.assertEqual(resp.recommendations[1].name, "Coastal Beach Shelter")
        self.assertEqual(resp.recommendations[1].route_status, RouteClassification.ROUTE_WITH_WARNINGS)

    @patch("app.services.unified_recommendation.assess_flood_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    @patch("app.services.unified_recommendation.calculate_evacuation_routes")
    def test_limited_recommendations_status(
        self,
        mock_calc_routes: MagicMock,
        mock_assess_suitability: MagicMock,
        mock_assess_flood: MagicMock,
    ) -> None:
        """Verifies LIMITED_RECOMMENDATIONS when only conditional suitability or routes with warnings are available."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        mock_fl = MagicMock()
        mock_fl.status = MagicMock(value="ACTIVE_FLOOD_OBSERVED")
        mock_fl.assessment_summary = "High rainfall hazard."
        mock_fl.observed_flooding = []
        mock_fl.official_warnings = []
        mock_fl.river_observations = []
        mock_fl.contributing_factors = []
        mock_fl.scientific_limitations = []
        mock_assess_flood.return_value = mock_fl

        cand_cond = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Temporary Community Hall",
            category="hall",
            official_designation=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            distance_km=2.5,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.CONDITIONALLY_SUITABLE,
            suitability_score=0.5,
            supporting_evidence=["Elevated plinth"],
            warnings=["Drainage overflow risk"],
            assessed_at=now,
        )

        mock_suit = MagicMock()
        mock_suit.assessed_locations = [cand_cond]
        mock_assess_suitability.return_value = mock_suit

        route_warn = RouteDetailItem(
            route_id=uuid.uuid4(),
            route_geometry=GeoJSONLineString(type="LineString", coordinates=[[91.7, 26.1], [91.72, 26.12]]),
            distance_m=2500.0,
            distance_km=2.5,
            estimated_time_seconds=250,
            estimated_time_minutes=4.0,
            route_status=RouteClassification.ROUTE_WITH_WARNINGS,
            hazard_exposure_level="MODERATE",
            hazard_intersections_count=1,
            hazard_intersections=[],
            warnings=["Surface waterlogging near crossing"],
            is_hard_excluded=False,
            routing_provider="OSRM (driving)",
            calculated_at=now,
        )
        mock_calc_routes.return_value = CalculateRouteResponse(
            success=True,
            route_status=RouteClassification.ROUTE_WITH_WARNINGS,
            origin={"latitude": 26.1, "longitude": 91.7},
            destination_id=cand_cond.location_id,
            disaster_type=CrisisType.FLOOD,
            primary_route=route_warn,
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=26.1,
            longitude=91.7,
            disaster_type=CrisisType.FLOOD,
        )

        self.assertTrue(resp.success)
        self.assertEqual(resp.status, UnifiedRecommendationStatus.LIMITED_RECOMMENDATIONS)

    @patch("app.services.unified_recommendation.assess_flood_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    def test_no_suitable_destination_found_status(
        self,
        mock_assess_suitability: MagicMock,
        mock_assess_flood: MagicMock,
    ) -> None:
        """Verifies NO_SUITABLE_DESTINATION_FOUND when all candidates are hard-excluded."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        mock_fl = MagicMock()
        mock_fl.status = MagicMock(value="ACTIVE_FLOOD_OBSERVED")
        mock_fl.assessment_summary = "Widespread inundation."
        mock_fl.observed_flooding = []
        mock_fl.official_warnings = []
        mock_fl.river_observations = []
        mock_fl.contributing_factors = []
        mock_fl.scientific_limitations = []
        mock_assess_flood.return_value = mock_fl

        # Candidate is EXCLUDED
        cand_exc = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Submerged Ground",
            category="park",
            official_designation=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            distance_km=1.0,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.EXCLUDED,
            suitability_score=0.0,
            hard_exclusion_status=True,
            hard_exclusion_reasons=["Directly submerged"],
            assessed_at=now,
        )

        mock_suit = MagicMock()
        mock_suit.assessed_locations = [cand_exc]
        mock_assess_suitability.return_value = mock_suit

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=26.1,
            longitude=91.7,
            disaster_type=CrisisType.FLOOD,
        )

        self.assertTrue(resp.success)
        self.assertEqual(resp.status, UnifiedRecommendationStatus.NO_SUITABLE_DESTINATION_FOUND)
        self.assertEqual(resp.total_suitable_destinations, 0)
        self.assertEqual(len(resp.recommendations), 0)

    @patch("app.services.unified_recommendation.assess_flood_hazard")
    @patch("app.services.unified_recommendation.assess_safe_locations_suitability")
    @patch("app.services.unified_recommendation.calculate_evacuation_routes")
    def test_no_route_available_status(
        self,
        mock_calc_routes: MagicMock,
        mock_assess_suitability: MagicMock,
        mock_assess_flood: MagicMock,
    ) -> None:
        """Verifies NO_ROUTE_AVAILABLE when suitable candidates exist but routing fails or routes are excluded."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        mock_fl = MagicMock()
        mock_fl.status = MagicMock(value="ACTIVE_FLOOD_OBSERVED")
        mock_fl.assessment_summary = "Flash flood warning."
        mock_fl.observed_flooding = []
        mock_fl.official_warnings = []
        mock_fl.river_observations = []
        mock_fl.contributing_factors = []
        mock_fl.scientific_limitations = []
        mock_assess_flood.return_value = mock_fl

        cand = LocationSuitabilityAssessmentItem(
            assessment_id=uuid.uuid4(),
            location_id=uuid.uuid4(),
            name="Safe Shelter High",
            category="shelter",
            official_designation=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            distance_km=5.0,
            disaster_type=CrisisType.FLOOD,
            suitability_classification=SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            suitability_score=0.9,
            supporting_evidence=["Reinforced concrete structure"],
            assessed_at=now,
        )

        mock_suit = MagicMock()
        mock_suit.assessed_locations = [cand]
        mock_assess_suitability.return_value = mock_suit

        # Routing service unavailable
        mock_calc_routes.return_value = CalculateRouteResponse(
            success=False,
            route_status=RouteClassification.ROUTING_SERVICE_UNAVAILABLE,
            origin={"latitude": 26.1, "longitude": 91.7},
            destination_id=cand.location_id,
            disaster_type=CrisisType.FLOOD,
            primary_route=None,
            comparison_summary="OSRM service unreachable",
            routing_provider="OSRM (driving)",
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

        resp = generate_unified_recommendations(
            session=mock_session,
            latitude=26.1,
            longitude=91.7,
            disaster_type=CrisisType.FLOOD,
        )

        self.assertTrue(resp.success)
        self.assertEqual(resp.status, UnifiedRecommendationStatus.NO_ROUTE_AVAILABLE)


if __name__ == "__main__":
    unittest.main()
