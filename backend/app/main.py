from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.alerts import router as alerts_router
from app.api.v1.cyclone_assessment import router as cyclone_assessment_router
from app.api.v1.disasters import router as disasters_router
from app.api.v1.earthquake_assessment import router as earthquake_assessment_router
from app.api.v1.flood_assessment import router as flood_assessment_router
from app.api.v1.health import router as health_router
from app.api.v1.ingestion import router as ingestion_router
from app.api.v1.location import router as location_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.recommendations import router as recommendations_router
from app.api.v1.routes import router as routes_router
from app.api.v1.safe_locations import router as safe_locations_router
from app.config import settings
from app.logging_config import configure_logging
from app.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.services.alert_service import (
    start_background_alert_monitor,
    stop_background_alert_monitor,
)

# Initialize production-ready structured logging
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Initializing %s in [%s] mode...", settings.app_name, settings.environment)
    if settings.alert_monitoring_enabled:
        start_background_alert_monitor(settings.alert_monitoring_interval_seconds)
    try:
        yield
    finally:
        logger.info("Gracefully shutting down %s...", settings.app_name)
        stop_background_alert_monitor()
        try:
            from app.database.session import engine
            engine.dispose()
            logger.info("Database connection pool disposed gracefully.")
        except Exception as exc:
            logger.warning("Error disposing database engine pool: %s", exc)


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
)

# 1. Defense-in-depth security headers
app.add_middleware(SecurityHeadersMiddleware)

# 2. Rate limiting middleware
app.add_middleware(
    RateLimitMiddleware,
    per_minute=settings.rate_limit_per_minute,
    enabled=settings.rate_limit_enabled,
)

# 3. Environment-aware CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API Routers
app.include_router(health_router)
app.include_router(location_router)
app.include_router(ingestion_router)
app.include_router(disasters_router)
app.include_router(earthquake_assessment_router)
app.include_router(flood_assessment_router)
app.include_router(cyclone_assessment_router)
app.include_router(safe_locations_router)
app.include_router(recommendations_router)
app.include_router(routes_router)
app.include_router(alerts_router)
app.include_router(notifications_router)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path != "/api/v1/location/validate":
        return await request_validation_exception_handler(request, exc)

    errors = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(errors)},
    )
