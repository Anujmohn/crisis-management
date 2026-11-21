from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.recommendations import assess_recommendations
from app.main import app
from app.models.core import CrisisEvent, DataSource
from app.models.enums import (
    AccessibilityStatus,
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    RecommendationRunStatus,
    RiskLevel,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import (
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
    SafeLocationSourceReference,
)
from app.schemas.recommendation import (
    AssessRecommendationsRequest,
    AssessRecommendationsResponse,
    SuitabilityClassification,
)
from app.services.recommendation_engine import (
    ENGINE_VERSION,
    SAFETY_DISCLAIMER,
    assess_safe_locations_suitability,
    evaluate_candidate_suitability,
    evaluate_cyclone_suitability,
    evaluate_earthquake_suitability,
    evaluate_flood_suitability,
)


class RecommendationAssessmentTests(unittest.TestCase):
    def test_recommendation_routes_registered(self) -> None:
        """Verifies OpenAPI schema registers POST /api/v1/recommendations/assess with correct schema constraints."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})

        self.assertIn("/api/v1/recommendations/assess", paths)
        post_op = paths["/api/v1/recommendations/assess"]["post"]
        self.assertIsNotNone(post_op)

        # Verify request body schema exists
        self.assertIn("requestBody", post_op)
        req_content = post_op["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("$ref", req_content)
        ref_name = req_content["$ref"].split("/")[-1]
        self.assertEqual(ref_name, "AssessRecommendationsRequest")

        schemas = openapi_schema["components"]["schemas"]
        req_schema = schemas["AssessRecommendationsRequest"]
        self.assertIn("latitude", req_schema["properties"])
        self.assertIn("longitude", req_schema["properties"])
        self.assertIn("disaster_type", req_schema["properties"])

    def test_earthquake_open_ground_flat_vs_steep(self) -> None:
        """Verifies earthquake suitability evaluates open ground safely on flat plains but flags steep terrain rockfall risks."""
        # 1. Flat terrain open ground (Patna Gangetic Plains: lat 25.5941, lon 85.1376)
        flat_open = SafeLocation(
            id=uuid.uuid4(),
            name="Central Public Park",
            category="PARK",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            structural_condition=None,
        )

        (
            cls_flat,
            is_exc_flat,
            hard_flat,
            ev_flat,
            haz_flat,
            warn_flat,
            miss_flat,
            score_flat,
            comp_flat,
        ) = evaluate_earthquake_suitability(loc=flat_open, lat=25.5941, lon=85.1376, distance_km=2.0)

        self.assertFalse(is_exc_flat)
        self.assertEqual(cls_flat, SuitabilityClassification.CONDITIONALLY_SUITABLE)
        self.assertTrue(any("skyward clearance" in e.lower() for e in ev_flat))
        self.assertTrue(any("power lines" in w.lower() or "façades" in w.lower() for w in warn_flat))
        self.assertGreater(score_flat, 0.4)

        # 2. Mountainous open ground (Uttarakhand Himalayas: lat 30.3165, lon 78.0322)
        hilly_open = SafeLocation(
            id=uuid.uuid4(),
            name="Mountain Ridge Playground",
            category="PLAYGROUND",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            structural_condition=None,
        )

        (
            cls_hilly,
            is_exc_hilly,
            hard_hilly,
            ev_hilly,
            haz_hilly,
            warn_hilly,
            miss_hilly,
            score_hilly,
            comp_hilly,
        ) = evaluate_earthquake_suitability(loc=hilly_open, lat=30.3165, lon=78.0322, distance_km=2.0)

        self.assertFalse(is_exc_hilly)
        self.assertEqual(cls_hilly, SuitabilityClassification.NOT_RECOMMENDED)
        self.assertTrue(any("landslide" in h.lower() for h in haz_hilly))
        self.assertTrue(any("slope failure" in w.lower() or "rockfall" in w.lower() for w in warn_hilly))

    def test_earthquake_building_structural_conditions(self) -> None:
        """Verifies earthquake evaluation differentiates sound verified buildings from poor or unknown buildings."""
        # 1. Poor structural condition
        poor_bldg = SafeLocation(
            id=uuid.uuid4(),
            name="Dilapidated Community Hall",
            category="COMMUNITY_CENTER",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            structural_condition=StructuralCondition.POOR,
        )
        cls_poor, is_exc_poor, _, _, haz_poor, warn_poor, _, score_poor, _ = evaluate_earthquake_suitability(
            loc=poor_bldg, lat=28.6139, lon=77.2090, distance_km=1.0
        )
        self.assertEqual(cls_poor, SuitabilityClassification.NOT_RECOMMENDED)
        self.assertTrue(any("collapse" in w.lower() for w in warn_poor))
        self.assertLessEqual(score_poor, 0.25)

        # 2. Good verified building
        good_bldg = SafeLocation(
            id=uuid.uuid4(),
            name="District Emergency Operation Centre",
            category="EMERGENCY_CENTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            structural_condition=StructuralCondition.GOOD,
        )
        cls_good, _, _, ev_good, _, _, _, score_good, _ = evaluate_earthquake_suitability(
            loc=good_bldg, lat=28.6139, lon=77.2090, distance_km=1.0
        )
        self.assertEqual(cls_good, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE)
        self.assertTrue(any("sound structural condition" in e.lower() for e in ev_good))
        self.assertGreater(score_good, 0.75)

        # 3. Building without structural assessment (None)
        unv_bldg = SafeLocation(
            id=uuid.uuid4(),
            name="Primary School Building",
            category="SCHOOL",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            structural_condition=None,
        )
        cls_unv, _, _, _, _, warn_unv, miss_unv, score_unv, _ = evaluate_earthquake_suitability(
            loc=unv_bldg, lat=28.6139, lon=77.2090, distance_km=1.0
        )
        self.assertEqual(cls_unv, SuitabilityClassification.CONDITIONALLY_SUITABLE)
        self.assertTrue(any("seismic condition assessment unavailable" in m.lower() for m in miss_unv))
        self.assertTrue(any("unverified" in w.lower() for w in warn_unv))

    def test_flood_active_inundation_hard_exclusion(self) -> None:
        """Verifies that locations in close proximity to active flood inundation are strictly EXCLUDED with score 0.0."""
        # Simulated active flood event at (26.18, 91.75)
        flood_event = MagicMock(spec=CrisisEvent)
        flood_event.id = uuid.uuid4()
        flood_event.title = "Brahmaputra Active Inundation Warning"
        flood_event.description = "Low-lying river plain submerged under floodwaters"
        flood_event.status = CrisisStatus.ACTIVE
        flood_event.affected_area = True
        # Mock location as GeoJSON point at (26.18, 91.75)
        flood_event.location = MagicMock()

        candidate = SafeLocation(
            id=uuid.uuid4(),
            name="Riverbank School",
            category="SCHOOL",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
        )

        with patch("app.services.recommendation_engine.safe_postgis_to_geojson") as mock_geo:
            # Set coordinates matching exactly 100 meters away
            mock_geo.return_value = {"type": "Point", "coordinates": [91.75, 26.18]}
            cls_res, is_exc, hard_reasons, _, haz, _, _, score, _ = evaluate_flood_suitability(
                loc=candidate,
                lat=26.1805,  # ~55 meters away
                lon=91.7505,
                distance_km=0.1,
                active_flood_events=[flood_event],
            )

        self.assertTrue(is_exc)
        self.assertEqual(cls_res, SuitabilityClassification.EXCLUDED)
        self.assertEqual(score, 0.0)
        self.assertTrue(any("active flood inundation zone" in r.lower() for r in hard_reasons))
        self.assertTrue(any("submerged" in h.lower() for h in haz))

    def test_flood_open_ground_vs_elevated_shelter(self) -> None:
        """Verifies that open grounds are NOT_RECOMMENDED for floods, while officially verified elevated shelters receive top suitability."""
        # 1. Open ground during flood
        open_ground = SafeLocation(
            id=uuid.uuid4(),
            name="Township Meadow Park",
            category="PARK",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        cls_open, is_exc_open, _, _, haz_open, warn_open, _, score_open, _ = evaluate_flood_suitability(
            loc=open_ground, lat=26.18, lon=91.75, distance_km=2.0, active_flood_events=[]
        )
        self.assertFalse(is_exc_open)
        self.assertEqual(cls_open, SuitabilityClassification.NOT_RECOMMENDED)
        self.assertTrue(any("runoff" in h.lower() or "retention" in h.lower() for h in haz_open))
        self.assertLess(score_open, 0.35)

        # 2. Officially verified elevated flood shelter
        flood_shelter = SafeLocation(
            id=uuid.uuid4(),
            name="Kaziranga Elevated Flood Shelter",
            category="OFFICIAL_FLOOD_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
        )
        cls_sh, is_exc_sh, _, ev_sh, _, _, _, score_sh, _ = evaluate_flood_suitability(
            loc=flood_shelter, lat=26.58, lon=93.17, distance_km=2.0, active_flood_events=[]
        )
        self.assertFalse(is_exc_sh)
        self.assertEqual(cls_sh, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE)
        self.assertTrue(any("elevated plinth" in e.lower() for e in ev_sh))
        self.assertGreater(score_sh, 0.75)

    def test_cyclone_coastal_open_area_hard_exclusion(self) -> None:
        """Verifies that coastal open areas during active cyclones are hard EXCLUDED with score 0.0 due to storm surge."""
        coastal_open = SafeLocation(
            id=uuid.uuid4(),
            name="Puri Beach Ground",
            category="OPEN_GROUND",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )

        active_cyclone = MagicMock(spec=CrisisEvent)
        active_cyclone.status = CrisisStatus.ACTIVE

        # Puri coordinates: lat 19.8135, lon 85.8312 (coastal, ~0.5 km from coast)
        cls_cyc, is_exc_cyc, hard_reasons, _, haz_cyc, _, _, score_cyc, _ = evaluate_cyclone_suitability(
            loc=coastal_open,
            lat=19.8135,
            lon=85.8312,
            distance_km=1.0,
            active_cyclone_events=[active_cyclone],
        )

        self.assertTrue(is_exc_cyc)
        self.assertEqual(cls_cyc, SuitabilityClassification.EXCLUDED)
        self.assertEqual(score_cyc, 0.0)
        self.assertTrue(any("coastal open area" in r.lower() for r in hard_reasons))
        self.assertTrue(any("storm surge" in r.lower() for r in hard_reasons))

    def test_cyclone_ncrmp_stilt_shelter_vs_unverified(self) -> None:
        """Verifies NCRMP multi-purpose cyclone shelters receive SUITABILITY_SUPPORTED_BY_EVIDENCE while unverified buildings get warnings."""
        # 1. Official NCRMP Shelter (e.g. Kendrapara, Odisha coast)
        ncrmp_shelter = SafeLocation(
            id=uuid.uuid4(),
            name="Kendrapara Multi-Purpose Cyclone Shelter",
            category="OFFICIAL_CYCLONE_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
        )
        cls_ncrmp, _, _, ev_ncrmp, _, _, _, score_ncrmp, _ = evaluate_cyclone_suitability(
            loc=ncrmp_shelter, lat=20.4285, lon=86.5812, distance_km=3.0, active_cyclone_events=[]
        )
        self.assertEqual(cls_ncrmp, SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE)
        self.assertTrue(any("multi-purpose cyclone shelter" in e.lower() for e in ev_ncrmp))
        self.assertTrue(any("stilt" in e.lower() for e in ev_ncrmp))
        self.assertGreater(score_ncrmp, 0.8)

        # 2. Unverified candidate building in coastal belt
        osm_bldg = SafeLocation(
            id=uuid.uuid4(),
            name="Coastal Commercial Complex",
            category="OTHER_CANDIDATE",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        cls_osm, _, _, _, haz_osm, warn_osm, _, score_osm, _ = evaluate_cyclone_suitability(
            loc=osm_bldg, lat=20.4285, lon=86.5812, distance_km=3.0, active_cyclone_events=[]
        )
        self.assertEqual(cls_osm, SuitabilityClassification.CONDITIONALLY_SUITABLE)
        self.assertTrue(any("wind-load rating is unrecorded" in w.lower() for w in warn_osm))
        self.assertTrue(any("coastal vulnerability zone" in h.lower() for h in haz_osm))

    def test_data_completeness_and_missing_information_never_zeroed(self) -> None:
        """Verifies that missing attributes are systematically captured in missing_information and not silently fabricated."""
        incomplete_loc = SafeLocation(
            id=uuid.uuid4(),
            name="Unmapped Community Hall",
            category="COMMUNITY_CENTER",
            designation_status=DesignationStatus.POTENTIAL,
            verification_status=VerificationStatus.UNVERIFIED,
            capacity=None,
            accessibility_status=None,
            structural_condition=None,
        )

        (
            cls_res,
            _,
            _,
            _,
            _,
            _,
            missing_info,
            score,
            completeness,
        ) = evaluate_candidate_suitability(
            loc=incomplete_loc,
            lat=28.6139,
            lon=77.2090,
            distance_km=2.0,
            disaster_type=CrisisType.EARTHQUAKE,
            active_events=[],
        )

        self.assertIn("Shelter capacity unrecorded", missing_info)
        self.assertIn("Disability/wheelchair accessibility unrecorded", missing_info)
        self.assertIn("Structural seismic condition assessment unavailable", missing_info)
        self.assertLess(completeness, 0.6)

    def test_assess_safe_locations_suitability_db_persistence(self) -> None:
        """Verifies that assess_safe_locations_suitability creates and commits RecommendationRun and LocationAssessment records."""
        loc1 = SafeLocation(
            id=uuid.uuid4(),
            name="Official Cyclone Shelter Alpha",
            category="OFFICIAL_CYCLONE_SHELTER",
            designation_status=DesignationStatus.OFFICIAL,
            verification_status=VerificationStatus.VERIFIED,
            capacity=1000,
            structural_condition=StructuralCondition.GOOD,
            accessibility_status=AccessibilityStatus.ACCESSIBLE,
        )
        loc1.source_references = []
        loc1.location = MagicMock()

        mock_session = MagicMock()

        # candidate query returns [(loc1, 2500.0)]
        mock_candidates = [(loc1, 2500.0)]
        mock_session.execute.return_value.all.return_value = mock_candidates

        # active events query returns empty list
        mock_session.scalars.return_value.all.return_value = []

        with patch("app.services.recommendation_engine.extract_location_coordinates") as mock_extract:
            mock_extract.return_value = (20.4285, 86.5812)
            with patch("app.services.recommendation_engine.geojson_to_postgis") as mock_g2p:
                mock_g2p.return_value = MagicMock()
                response = assess_safe_locations_suitability(
                    session=mock_session,
                    latitude=20.4000,
                    longitude=86.5500,
                    disaster_type=CrisisType.CYCLONE,
                    search_radius_km=25.0,
                    limit=50,
                )

        self.assertIsInstance(response, AssessRecommendationsResponse)
        self.assertEqual(response.total_candidates_assessed, 1)
        self.assertEqual(response.total_potentially_suitable, 1)
        self.assertEqual(len(response.assessed_locations), 1)

        # Verify RecommendationRun was added
        added_objs = [call[0][0] for call in mock_session.add.call_args_list]
        rec_runs = [obj for obj in added_objs if isinstance(obj, RecommendationRun)]
        loc_assessments = [obj for obj in added_objs if isinstance(obj, LocationAssessment)]

        self.assertEqual(len(rec_runs), 1)
        self.assertEqual(len(loc_assessments), 1)
        self.assertEqual(rec_runs[0].status, RecommendationRunStatus.COMPLETED)
        self.assertEqual(rec_runs[0].engine_version, ENGINE_VERSION)
        self.assertEqual(loc_assessments[0].safe_location_id, loc1.id)
        self.assertEqual(loc_assessments[0].recommendation_run_id, rec_runs[0].id)
        self.assertEqual(loc_assessments[0].risk_level, RiskLevel.LOW)

        # Verify session.commit() was called
        mock_session.commit.assert_called_once()

    def test_api_endpoint_invokes_recommendation_engine(self) -> None:
        """Verifies POST /api/v1/recommendations/assess endpoint function delegates cleanly to assess_safe_locations_suitability."""
        mock_session = MagicMock()
        req = AssessRecommendationsRequest(
            latitude=28.6139,
            longitude=77.2090,
            disaster_type=CrisisType.EARTHQUAKE,
            search_radius_km=15.0,
            limit=20,
        )

        with patch("app.api.v1.recommendations.assess_safe_locations_suitability") as mock_service:
            mock_resp = AssessRecommendationsResponse(
                success=True,
                recommendation_run_id=uuid.uuid4(),
                user_location={"latitude": 28.6139, "longitude": 77.2090},
                disaster_type=CrisisType.EARTHQUAKE,
                total_candidates_assessed=0,
                assessed_locations=[],
                assessment_methodology="Test Methodology",
                safety_disclaimer=SAFETY_DISCLAIMER,
            )
            mock_service.return_value = mock_resp

            res = assess_recommendations(request=req, session=mock_session)
            self.assertEqual(res.disaster_type, CrisisType.EARTHQUAKE)
            mock_service.assert_called_once_with(
                session=mock_session,
                latitude=28.6139,
                longitude=77.2090,
                disaster_type=CrisisType.EARTHQUAKE,
                crisis_event_id=None,
                search_radius_km=15.0,
                limit=20,
            )


if __name__ == "__main__":
    unittest.main()
