from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import Request, Response
from pydantic import SecretStr

from app.api.v1.health import (
    database_health_check,
    detailed_health_check,
    health_check,
    readiness_check,
)
from app.config import Settings
from app.main import app
from app.middleware import RateLimitMiddleware, SecurityHeadersMiddleware


class ProductionHealthAndSecurityTests(unittest.TestCase):
    def test_liveness_health_probe(self) -> None:
        """Verifies health_check returns 200 payload immediately without database queries."""
        data = health_check()
        self.assertEqual(data["status"], "ok")
        self.assertIn("app", data)
        self.assertIn("environment", data)

    def test_readiness_probe_database_success(self) -> None:
        """Verifies readiness_check returns status ready when database and PostGIS are available."""
        mock_session = MagicMock()
        mock_session.execute.return_value = None
        mock_session.scalar.return_value = "POSTGIS=\"3.4.0\""

        data = readiness_check(session=mock_session)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["database"], "connected")
        self.assertTrue(data["postgis"])

    def test_detailed_health_probe_operational_breakdown(self) -> None:
        """Verifies detailed_health_check returns comprehensive multi-service diagnostics."""
        mock_session = MagicMock()
        mock_session.execute.return_value = MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        mock_session.scalar.side_effect = ["3.4.0", 15, 12, 45]

        resp = detailed_health_check(session=mock_session)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body.decode("utf-8"))
        self.assertIn(data["status"], ("HEALTHY", "DEGRADED"))
        self.assertIn("application", data["checks"])
        self.assertIn("database", data["checks"])
        self.assertIn("data_sources", data["checks"])
        self.assertIn("routing_service", data["checks"])
        self.assertIn("notifications", data["checks"])
        self.assertIn("disclaimer", data["checks"])
        self.assertIn("NDMA", data["checks"]["disclaimer"])

    def test_health_routes_registered_in_openapi(self) -> None:
        """Verifies health and readiness probes are properly registered in OpenAPI paths."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})
        self.assertIn("/health", paths)
        self.assertIn("/health/ready", paths)
        self.assertIn("/health/detailed", paths)
        self.assertIn("/health/database", paths)

    def test_security_headers_middleware(self) -> None:
        """Verifies SecurityHeadersMiddleware attaches standard defense-in-depth headers."""
        middleware = SecurityHeadersMiddleware(app=MagicMock())
        mock_request = MagicMock(spec=Request)
        mock_request.url.scheme = "https"

        mock_response = Response(content="test")

        async def run_dispatch():
            async def call_next(req):
                return mock_response
            return await middleware.dispatch(mock_request, call_next)

        import asyncio
        resp = asyncio.run(run_dispatch())
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("X-XSS-Protection"), "1; mode=block")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertEqual(
            resp.headers.get("Strict-Transport-Security"),
            "max-age=31536000; includeSubDomains",
        )

    def test_rate_limiting_middleware_exemption(self) -> None:
        """Verifies health endpoints are exempt from rate limiting."""
        middleware = RateLimitMiddleware(app=MagicMock(), per_minute=10, enabled=True)
        mock_request = MagicMock(spec=Request)
        mock_request.url.path = "/health"

        mock_response = Response(content="ok")

        async def run_dispatch():
            async def call_next(req):
                return mock_response
            return await middleware.dispatch(mock_request, call_next)

        import asyncio
        resp = asyncio.run(run_dispatch())
        self.assertEqual(resp.status_code, 200)

    def test_production_environment_validation_rejects_default_credentials(self) -> None:
        """Verifies production settings validator rejects default/placeholder database passwords."""
        with self.assertRaises(ValueError) as ctx:
            Settings(
                environment="production",
                db_host="db.production.internal",
                db_name="disaster_safety_prod",
                db_user="disaster_admin",
                db_password=SecretStr("replace_with_your_local_password"),
            )
        self.assertIn("Insecure or default database password", str(ctx.exception))

    def test_production_environment_validation_rejects_db_echo(self) -> None:
        """Verifies production settings validator rejects SQL echo in production."""
        with self.assertRaises(ValueError) as ctx:
            Settings(
                environment="production",
                db_host="db.production.internal",
                db_name="disaster_safety_prod",
                db_user="disaster_admin",
                db_password=SecretStr("StrongP@ssw0rd2026!SecureKey"),
                db_echo=True,
            )
        self.assertIn("db_echo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
