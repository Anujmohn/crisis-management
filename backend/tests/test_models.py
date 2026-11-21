import unittest
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Numeric, UniqueConstraint, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import configure_mappers

from app.database.base import Base
from app.models import (
    CrisisEvent,
    CrisisSourceReport,
    DataSource,
    EvacuationRoute,
    HazardObservation,
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
    SafeLocationSourceReference,
)


class ModelSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configure_mappers()

    def test_exact_model_tables_and_no_user_location_table(self) -> None:
        expected = {
            "data_sources",
            "crisis_events",
            "crisis_source_reports",
            "hazard_observations",
            "safe_locations",
            "safe_location_source_references",
            "recommendation_runs",
            "location_assessments",
            "evacuation_routes",
            "device_alert_profiles",
            "alert_notifications",
            "alert_sync_logs",
        }
        self.assertEqual(set(Base.metadata.tables), expected)
        self.assertNotIn("user_locations", Base.metadata.tables)

    def test_geometry_columns_have_required_type_srid_and_gist_index(self) -> None:
        geometry_columns = (
            (CrisisEvent, "location", "POINT", True),
            (CrisisEvent, "affected_area", "MULTIPOLYGON", True),
            (HazardObservation, "geometry", "GEOMETRY", True),
            (SafeLocation, "location", "POINT", False),
            (SafeLocation, "boundary", "GEOMETRY", True),
            (RecommendationRun, "user_location", "POINT", False),
            (EvacuationRoute, "route_geometry", "LINESTRING", False),
        )
        for model, column_name, geometry_type, nullable in geometry_columns:
            with self.subTest(model=model.__name__, column=column_name):
                column = model.__table__.c[column_name]
                self.assertIsInstance(column.type, Geometry)
                self.assertEqual(column.type.geometry_type, geometry_type)
                self.assertEqual(column.type.srid, 4326)
                self.assertEqual(column.nullable, nullable)
                self.assertTrue(
                    any(
                        index.dialect_options["postgresql"].get("using") == "gist"
                        and column.name in index.columns
                        for index in model.__table__.indexes
                    )
                )

    def test_foreign_keys_and_relationships(self) -> None:
        expected_relationships = {
            CrisisSourceReport: {"crisis_event", "data_source"},
            HazardObservation: {"crisis_event", "data_source"},
            SafeLocationSourceReference: {"safe_location", "data_source"},
            RecommendationRun: {"crisis_event", "assessments"},
            LocationAssessment: {
                "recommendation_run",
                "safe_location",
                "routes",
            },
            EvacuationRoute: {"location_assessment"},
        }
        for model, relationship_names in expected_relationships.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(
                    set(inspect(model).relationships.keys()),
                    relationship_names,
                )

        self.assertTrue(HazardObservation.__table__.c.crisis_event_id.nullable)
        self.assertFalse(CrisisSourceReport.__table__.c.crisis_event_id.nullable)
        self.assertFalse(HazardObservation.__table__.c.data_source_id.nullable)
        self.assertFalse(
            SafeLocationSourceReference.__table__.c.safe_location_id.nullable
        )

    def test_unique_constraints_and_coordinate_checks(self) -> None:
        self.assertTrue(DataSource.__table__.c.code.unique)
        report_unique = {
            constraint.name
            for constraint in CrisisSourceReport.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        self.assertIn(
            "uq_crisis_source_reports_source_external_id",
            report_unique,
        )
        self.assertIn(
            "uq_location_assessments_run_location",
            {
                constraint.name
                for constraint in LocationAssessment.__table__.constraints
                if isinstance(constraint, UniqueConstraint)
            },
        )

        spatial_tables = (
            CrisisEvent,
            HazardObservation,
            SafeLocation,
            RecommendationRun,
            EvacuationRoute,
        )
        for model in spatial_tables:
            with self.subTest(model=model.__name__):
                checks = [
                    str(constraint.sqltext)
                    for constraint in model.__table__.constraints
                    if getattr(constraint, "sqltext", None) is not None
                ]
                self.assertTrue(any("ST_MakeEnvelope" in check for check in checks))

    def test_timestamp_timezone_jsonb_and_numeric_types(self) -> None:
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    with self.subTest(table=table.name, column=column.name):
                        self.assertTrue(column.type.timezone)

        self.assertIsInstance(CrisisSourceReport.__table__.c.raw_payload.type, JSONB)
        self.assertIsInstance(HazardObservation.__table__.c.properties.type, JSONB)
        self.assertIsInstance(LocationAssessment.__table__.c.factors.type, JSONB)
        self.assertIsInstance(EvacuationRoute.__table__.c.hazard_intersections.type, JSONB)
        self.assertIsInstance(HazardObservation.__table__.c.value_numeric.type, Numeric)
        self.assertEqual(
            HazardObservation.__table__.c.value_numeric.type.asdecimal,
            True,
        )
        self.assertIsInstance(
            LocationAssessment.__table__.c.suitability_score.type,
            Numeric,
        )
        self.assertEqual(
            LocationAssessment.__table__.c.suitability_score.type.precision,
            5,
        )
        self.assertEqual(
            LocationAssessment.__table__.c.suitability_score.type.scale,
            4,
        )

    def test_aware_datetime_and_decimal_python_types(self) -> None:
        self.assertEqual(
            CrisisEvent.__table__.c.created_at.type.python_type,
            datetime,
        )
        self.assertEqual(
            HazardObservation.__table__.c.value_numeric.type.python_type,
            Decimal,
        )
