from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.alert import (
    DevicePreferencesRead,
    DevicePreferencesUpdate,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    NotificationHistoryResponse,
)
from app.services.alert_service import (
    get_device_notification_history,
    get_device_preferences,
    mark_notification_read,
    register_or_update_device,
    unregister_device,
    update_device_preferences,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get(
    "/preferences",
    response_model=DevicePreferencesRead,
    status_code=status.HTTP_200_OK,
    summary="Get disaster notification preferences for a device installation",
    description="Retrieves persistent alert preferences (earthquake, flood, cyclone, radius, sound) for an installation.",
)
def get_preferences_endpoint(
    device_id: Annotated[str, Query(min_length=1, max_length=128, description="Anonymous device installation ID")],
    session: Annotated[Session, Depends(get_db)] = None,
) -> DevicePreferencesRead:
    return get_device_preferences(session=session, device_id=device_id)


@router.put(
    "/preferences",
    response_model=DevicePreferencesRead,
    status_code=status.HTTP_200_OK,
    summary="Update disaster notification preferences for a device installation",
    description="Updates persistent alert preferences (categories, critical/advisories, notification radius, sound).",
)
def update_preferences_endpoint(
    update: DevicePreferencesUpdate,
    session: Annotated[Session, Depends(get_db)] = None,
) -> DevicePreferencesRead:
    return update_device_preferences(session=session, update=update)


@router.post(
    "/devices/register",
    response_model=DeviceRegisterResponse,
    status_code=status.HTTP_200_OK,
    summary="Register or update mobile device push token and preferences",
    description=(
        "Registers an anonymous device installation with an Expo push token, platform, and alert preferences. "
        "Allows coarse location registration for spatial relevance checking without requiring user accounts."
    ),
)
def register_device_endpoint(
    request: DeviceRegisterRequest,
    session: Annotated[Session, Depends(get_db)] = None,
) -> DeviceRegisterResponse:
    logger.info("Device registration request received for device_id=%s, platform=%s", request.device_id, request.platform)
    return register_or_update_device(session=session, request=request)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_200_OK,
    summary="Unregister device push token and deactivate notification profile",
    description="Deactivates alert dispatching and clears push token for the specified installation.",
)
def unregister_device_endpoint(
    device_id: str,
    session: Annotated[Session, Depends(get_db)] = None,
) -> dict[str, Any]:
    success = unregister_device(session=session, device_id=device_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_id}' not found.",
        )
    return {"success": True, "message": f"Device '{device_id}' unregistered successfully."}


@router.get(
    "/history",
    response_model=NotificationHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Get in-app notification history and unread badge count for a device",
    description="Retrieves historical disaster alerts dispatched to the device, delivery status, and unread counts.",
)
def get_notification_history_endpoint(
    device_id: Annotated[str, Query(min_length=1, max_length=128, description="Device installation ID")],
    limit: Annotated[int, Query(ge=1, le=100, description="Max notifications to retrieve")] = 50,
    unread_only: Annotated[bool, Query(description="Filter to unread notifications only")] = False,
    session: Annotated[Session, Depends(get_db)] = None,
) -> NotificationHistoryResponse:
    return get_device_notification_history(
        session=session,
        device_id=device_id,
        limit=limit,
        unread_only=unread_only,
    )


@router.patch(
    "/history/{notification_id}/read",
    status_code=status.HTTP_200_OK,
    summary="Mark a specific notification as read",
    description="Updates the read status of an in-app notification record.",
)
def mark_notification_read_endpoint(
    notification_id: UUID,
    device_id: Annotated[str | None, Query(description="Optional device ID for ownership verification")] = None,
    session: Annotated[Session, Depends(get_db)] = None,
) -> dict[str, Any]:
    success = mark_notification_read(session=session, notification_id=notification_id, device_id=device_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification '{notification_id}' not found.",
        )
    return {"success": True, "message": f"Notification '{notification_id}' marked as read."}
