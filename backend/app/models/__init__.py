"""Import ORM model modules here so Alembic can register their metadata."""

from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.notifications import (
    AlertNotificationRecord,
    AlertSyncLog,
    DeviceAlertProfile,
)
from app.models.recommendations import (
    EvacuationRoute,
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
    SafeLocationSourceReference,
)

__all__ = [
    "AlertNotificationRecord",
    "AlertSyncLog",
    "CrisisEvent",
    "CrisisSourceReport",
    "DataSource",
    "DeviceAlertProfile",
    "EvacuationRoute",
    "HazardObservation",
    "LocationAssessment",
    "RecommendationRun",
    "SafeLocation",
    "SafeLocationSourceReference",
]
