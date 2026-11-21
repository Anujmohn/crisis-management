from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database.session import get_db
from app.models.notifications import AlertNotificationRecord, AlertSyncLog, DeviceAlertProfile

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Operational health statuses reflect software system connectivity only. "
    "System availability does NOT guarantee absence of natural disaster hazards. "
    "Always obey official instructions issued by NDMA, SDMA, and local emergency personnel."
)


@router.get(
    "/health",
    summary="Liveness probe",
    description="Immediate lightweight status probe for container orchestrators and load balancers. Zero database load.",
)
def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
    }


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description="Validates database connectivity and PostGIS extension readiness before routing user traffic.",
)
def readiness_check(
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        session.execute(text("SELECT 1"))
        postgis_version = session.scalar(text("SELECT postgis_full_version()"))
    except SQLAlchemyError as exc:
        logger.exception("Readiness check failed: database unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database or PostGIS extension is unavailable",
        ) from exc

    if postgis_version is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostGIS extension is not installed or enabled",
        )

    return {
        "status": "ready",
        "database": "connected",
        "postgis": True,
    }


@router.get(
    "/health/database",
    summary="Database health probe (legacy)",
    description="Checks database and PostGIS version information.",
)
def database_health_check(
    session: Session = Depends(get_db),
) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
        postgis_version = session.scalar(text("SELECT postgis_full_version()"))
    except SQLAlchemyError as exc:
        logger.exception("Database health check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database or PostGIS is unavailable",
        ) from exc

    if postgis_version is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database or PostGIS is unavailable",
        )

    return {
        "status": "ok",
        "database": "connected",
        "postgis_version": str(postgis_version),
    }


@router.get(
    "/health/detailed",
    summary="Comprehensive operational diagnostic probe",
    description=(
        "Deep operational probe that inspects application runtime, PostgreSQL/PostGIS connectivity, "
        "background disaster data-source sync freshness, routing service reachability, and notification delivery state."
    ),
)
def detailed_health_check(
    session: Session = Depends(get_db),
) -> JSONResponse:
    checks: dict[str, Any] = {
        "application": {
            "name": settings.app_name,
            "environment": settings.environment,
            "status": "healthy",
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
        },
        "database": {
            "status": "unknown",
            "host": settings.db_host,
            "database": settings.db_name,
            "postgis": None,
        },
        "data_sources": {
            "status": "unknown",
            "background_monitoring_enabled": settings.alert_monitoring_enabled,
            "monitoring_interval_seconds": settings.alert_monitoring_interval_seconds,
            "last_sync_at": None,
            "last_sync_status": "NONE",
            "recent_sync_count": 0,
            "recent_errors": [],
        },
        "routing_service": {
            "provider": "OSRM (Open Source Routing Machine)",
            "configured_base_url": settings.osrm_base_url,
            "timeout_seconds": settings.osrm_timeout_seconds,
            "status": "CONFIGURED",
        },
        "notifications": {
            "service": "Expo Push Notification Gateway",
            "registered_devices": 0,
            "active_devices": 0,
            "total_notifications_logged": 0,
            "status": "OPERATIONAL",
        },
        "disclaimer": DISCLAIMER,
    }

    overall_status = "HEALTHY"
    status_code = status.HTTP_200_OK

    # 1. Database & PostGIS
    try:
        session.execute(text("SELECT 1"))
        pg_ver = session.scalar(text("SELECT postgis_version()"))
        checks["database"]["status"] = "connected"
        checks["database"]["postgis"] = str(pg_ver) if pg_ver else "INSTALLED"
    except Exception as exc:
        logger.warning("Database check failed in /health/detailed: %s", exc)
        checks["database"]["status"] = "unhealthy"
        checks["database"]["error"] = "Database connection failed"
        overall_status = "UNHEALTHY"
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # 2. Data Sources Synchronization Health
    if checks["database"]["status"] == "connected":
        try:
            recent_logs = (
                session.execute(
                    select(AlertSyncLog)
                    .order_by(AlertSyncLog.started_at.desc())
                    .limit(5)
                )
                .scalars()
                .all()
            )
            if recent_logs:
                last_log = recent_logs[0]
                checks["data_sources"]["last_sync_at"] = (
                    last_log.completed_at.isoformat() if last_log.completed_at else None
                )
                checks["data_sources"]["last_sync_status"] = last_log.status
                checks["data_sources"]["recent_sync_count"] = len(recent_logs)
                errors = [
                    f"{log.source_code}: {log.error_message}"
                    for log in recent_logs
                    if log.error_message and log.status != "SUCCESS"
                ]
                checks["data_sources"]["recent_errors"] = errors
                if any(log.status == "FAILED" for log in recent_logs):
                    checks["data_sources"]["status"] = "DEGRADED"
                    if overall_status == "HEALTHY":
                        overall_status = "DEGRADED"
                else:
                    checks["data_sources"]["status"] = "HEALTHY"
            else:
                checks["data_sources"]["status"] = "IDLE"
        except Exception as exc:
            checks["data_sources"]["status"] = "UNAVAILABLE"
            checks["data_sources"]["error"] = str(exc)

        # 3. Notification Profiles & Delivery Stats
        try:
            total_devs = session.scalar(select(func.count(DeviceAlertProfile.id))) or 0
            active_devs = (
                session.scalar(
                    select(func.count(DeviceAlertProfile.id)).where(
                        DeviceAlertProfile.is_active.is_(True)
                    )
                )
                or 0
            )
            total_notes = (
                session.scalar(select(func.count(AlertNotificationRecord.id))) or 0
            )
            checks["notifications"]["registered_devices"] = total_devs
            checks["notifications"]["active_devices"] = active_devs
            checks["notifications"]["total_notifications_logged"] = total_notes
        except Exception as exc:
            checks["notifications"]["status"] = "UNAVAILABLE"
            checks["notifications"]["error"] = str(exc)

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        },
    )
