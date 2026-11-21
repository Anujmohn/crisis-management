from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.api.v1.alerts import get_nearby_alerts_endpoint, sync_alerts_endpoint
from app.api.v1.notifications import (
    get_notification_history_endpoint,
    get_preferences_endpoint,
    mark_notification_read_endpoint,
    register_device_endpoint,
    unregister_device_endpoint,
    update_preferences_endpoint,
)
from app.main import app
from app.models.core import CrisisEvent, CrisisSourceReport, DataSource
from app.models.enums import (
    AlertClassification,
    AlertSourceKind,
    CrisisStatus,
    CrisisType,
    SeverityLevel,
)
from app.models.notifications import (
    AlertNotificationRecord,
    AlertSyncLog,
    DeviceAlertProfile,
)
from app.schemas.alert import (
    AlertItem,
    AlertSyncRequest,
    DevicePreferencesUpdate,
    DeviceRegisterRequest,
)
from app.services.alert_service import (
    ALERT_SAFETY_DISCLAIMER,
    classify_report_and_event,
    dispatch_alert_notifications_for_report,
    get_device_notification_history,
    get_device_preferences,
    get_nearby_alerts,
    mark_notification_read,
    register_or_update_device,
    send_expo_push_notification,
    sync_active_disaster_alerts,
    unregister_device,
    update_device_preferences,
)


class AlertsAndNotificationsTests(unittest.TestCase):
    def test_api_endpoints_registered_in_openapi(self) -> None:
        """Verifies that all required alert and notification endpoints are registered in OpenAPI schema."""
        openapi_schema = app.openapi()
        paths = openapi_schema.get("paths", {})

        # 1. Alert retrieval
        self.assertIn("/api/v1/alerts/nearby", paths)
        self.assertIn("get", paths["/api/v1/alerts/nearby"])

        # 2. Alert synchronization
        self.assertIn("/api/v1/alerts/sync", paths)
        self.assertIn("post", paths["/api/v1/alerts/sync"])

        # 3. Notification preferences
        self.assertIn("/api/v1/notifications/preferences", paths)
        self.assertIn("get", paths["/api/v1/notifications/preferences"])
        self.assertIn("put", paths["/api/v1/notifications/preferences"])

        # 4. Device registration
        self.assertIn("/api/v1/notifications/devices/register", paths)
        self.assertIn("post", paths["/api/v1/notifications/devices/register"])

        # 5. Device unregistration
        self.assertIn("/api/v1/notifications/devices/{device_id}", paths)
        self.assertIn("delete", paths["/api/v1/notifications/devices/{device_id}"])

        # 6. Notification history
        self.assertIn("/api/v1/notifications/history", paths)
        self.assertIn("get", paths["/api/v1/notifications/history"])
        self.assertIn("/api/v1/notifications/history/{notification_id}/read", paths)
        self.assertIn("patch", paths["/api/v1/notifications/history/{notification_id}/read"])

    def test_alert_classification_and_source_kind_distinction(self) -> None:
        """Verifies distinct classification of official government warnings vs observations vs updates."""
        now = datetime.now(timezone.utc)

        # A. Official warning from NDMA SACHET
        sachet_src = MagicMock(spec=DataSource)
        sachet_src.code = "NDMA_SACHET"
        sachet_rep = MagicMock(spec=CrisisSourceReport)
        sachet_rep.data_source = sachet_src
        sachet_rep.source_title = "Red Alert: Severe Cyclone Warning"
        sachet_rep.source_severity = "Severe"
        sachet_rep.source_status = "Active"
        sachet_rep.valid_until = now + timedelta(hours=12)

        ev_cyclone = MagicMock(spec=CrisisEvent)
        ev_cyclone.status = CrisisStatus.ACTIVE

        cls_warn, kind_warn = classify_report_and_event(sachet_rep, ev_cyclone, is_new=True)
        self.assertEqual(cls_warn, AlertClassification.OFFICIAL_WARNING)
        self.assertEqual(kind_warn, AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING)

        # B. Official observation from NCS Earthquake monitoring
        ncs_src = MagicMock(spec=DataSource)
        ncs_src.code = "NCS_SEISMO"
        ncs_rep = MagicMock(spec=CrisisSourceReport)
        ncs_rep.data_source = ncs_src
        ncs_rep.source_title = "Earthquake M4.8 Kangra"
        ncs_rep.source_severity = "4.8"
        ncs_rep.source_status = "Observed"
        ncs_rep.valid_until = now + timedelta(hours=48)

        ev_eq = MagicMock(spec=CrisisEvent)
        ev_eq.status = CrisisStatus.ACTIVE

        cls_obs, kind_obs = classify_report_and_event(ncs_rep, ev_eq, is_new=True)
        self.assertEqual(cls_obs, AlertClassification.NEW_DISASTER_EVENT)
        self.assertEqual(kind_obs, AlertSourceKind.OFFICIAL_OBSERVATION)

        # C. Warning escalation from Moderate to Extreme
        sachet_rep.source_severity = "Extreme"
        cls_esc, kind_esc = classify_report_and_event(
            sachet_rep, ev_cyclone, is_updated=True, previous_severity="Moderate"
        )
        self.assertEqual(cls_esc, AlertClassification.WARNING_ESCALATION)
        self.assertEqual(kind_esc, AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING)

        # D. Expired warning
        sachet_rep.valid_until = now - timedelta(hours=1)
        cls_exp, kind_exp = classify_report_and_event(sachet_rep, ev_cyclone)
        self.assertEqual(cls_exp, AlertClassification.WARNING_EXPIRED)

        # E. Cancelled warning
        sachet_rep.valid_until = now + timedelta(hours=6)
        sachet_rep.source_status = "Cancelled"
        cls_can, kind_can = classify_report_and_event(sachet_rep, ev_cyclone)
        self.assertEqual(cls_can, AlertClassification.WARNING_CANCELLED)

    def test_device_registration_and_preference_management(self) -> None:
        """Verifies registering anonymous device, updating preferences, and unregistering without user authentication."""
        mock_session = MagicMock()
        mock_session.scalars.return_value.first.return_value = None  # New device

        # 1. Register device
        reg_req = DeviceRegisterRequest(
            device_id="device-inst-uuid-1234",
            push_token="ExponentPushToken[mock-expo-push-token-test]",
            platform="android",
            alert_earthquake=True,
            alert_flood=True,
            alert_cyclone=False,  # User turned off cyclone alerts
            notification_radius_km=30.0,
            sound_enabled=True,
            latitude=28.6139,
            longitude=77.2090,
        )

        resp = register_or_update_device(session=mock_session, request=reg_req)
        self.assertTrue(resp.success)
        self.assertEqual(resp.device_id, "device-inst-uuid-1234")
        self.assertEqual(resp.preferences.notification_radius_km, 30.0)
        self.assertFalse(resp.preferences.alert_cyclone)
        self.assertTrue(resp.preferences.alert_earthquake)
        mock_session.commit.assert_called()

        # 2. Get preferences
        existing_profile = MagicMock(spec=DeviceAlertProfile)
        existing_profile.device_id = "device-inst-uuid-1234"
        existing_profile.push_token = "ExponentPushToken[mock-expo-push-token-test]"
        existing_profile.platform = "android"
        existing_profile.alert_earthquake = True
        existing_profile.alert_flood = True
        existing_profile.alert_cyclone = False
        existing_profile.alert_critical = True
        existing_profile.alert_advisory = True
        existing_profile.notification_radius_km = Decimal("30.0")
        existing_profile.sound_enabled = True
        existing_profile.is_active = True
        existing_profile.updated_at = datetime.now(timezone.utc)
        mock_session.scalars.return_value.first.return_value = existing_profile

        prefs = get_device_preferences(session=mock_session, device_id="device-inst-uuid-1234")
        self.assertEqual(prefs.device_id, "device-inst-uuid-1234")
        self.assertEqual(prefs.notification_radius_km, 30.0)

        # 3. Update preferences (re-enable cyclone alerts, expand radius to 75 km)
        up_req = DevicePreferencesUpdate(
            device_id="device-inst-uuid-1234",
            alert_cyclone=True,
            notification_radius_km=75.0,
        )
        updated_prefs = update_device_preferences(session=mock_session, update=up_req)
        self.assertEqual(updated_prefs.device_id, "device-inst-uuid-1234")
        self.assertTrue(existing_profile.alert_cyclone)
        self.assertEqual(existing_profile.notification_radius_km, Decimal("75.0"))

        # 4. Unregister device
        unreg_success = unregister_device(session=mock_session, device_id="device-inst-uuid-1234")
        self.assertTrue(unreg_success)
        self.assertFalse(existing_profile.is_active)
        self.assertIsNone(existing_profile.push_token)

    def test_expo_push_token_detection_and_simulated_dispatch(self) -> None:
        """Verifies send_expo_push_notification correctly detects simulated tokens vs calling push API."""
        # Non-Expo token (e.g. simulated development string)
        success_sim, status_sim, detail_sim = send_expo_push_notification(
            push_token="dev-simulated-token-123",
            title="Flood Warning",
            body="Rising river levels",
        )
        self.assertFalse(success_sim)
        self.assertEqual(status_sim, "SIMULATED")
        self.assertIn("reason", detail_sim)

        # Expo token format with network mock
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"data": [{"status": "ok", "id": "push-ticket-id-99"}]}'
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            success_live, status_live, detail_live = send_expo_push_notification(
                push_token="ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]",
                title="Cyclone Warning",
                body="Gale wind alert",
            )
            self.assertTrue(success_live)
            self.assertEqual(status_live, "DELIVERED")
            self.assertEqual(detail_live["data"][0]["status"], "ok")

    def test_dispatch_notifications_respects_user_category_preferences(self) -> None:
        """Verifies that an alert for FLOOD is dispatched only to devices that have FLOOD alerts enabled."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        # Device A: has flood alerts ENABLED
        dev_a = MagicMock(spec=DeviceAlertProfile)
        dev_a.device_id = "device-a-subscribed"
        dev_a.push_token = "ExponentPushToken[mock-token-a]"
        dev_a.is_active = True
        dev_a.alert_earthquake = True
        dev_a.alert_flood = True
        dev_a.alert_cyclone = True
        dev_a.alert_critical = True
        dev_a.alert_advisory = True
        dev_a.notification_radius_km = Decimal("50.0")
        dev_a.last_known_location = None  # No location restriction
        dev_a.sound_enabled = True

        # Device B: has flood alerts DISABLED
        dev_b = MagicMock(spec=DeviceAlertProfile)
        dev_b.device_id = "device-b-unsubscribed"
        dev_b.push_token = "ExponentPushToken[mock-token-b]"
        dev_b.is_active = True
        dev_b.alert_earthquake = True
        dev_b.alert_flood = False  # Disabled!
        dev_b.alert_cyclone = True
        dev_b.alert_critical = True
        dev_b.alert_advisory = True
        dev_b.notification_radius_km = Decimal("50.0")
        dev_b.last_known_location = None
        dev_b.sound_enabled = True

        mock_session.scalars.return_value.all.return_value = [dev_a, dev_b]
        mock_session.scalars.return_value.first.return_value = None  # No previous duplicate

        # Event: FLOOD
        event_fl = MagicMock(spec=CrisisEvent)
        event_fl.id = uuid.uuid4()
        event_fl.crisis_type = CrisisType.FLOOD
        event_fl.title = "Severe Inundation Alert"
        event_fl.description = "Brahmaputra basin water level rising"
        event_fl.location = None
        event_fl.affected_area = None

        report_fl = MagicMock(spec=CrisisSourceReport)
        report_fl.id = uuid.uuid4()
        report_fl.source_title = "Severe Inundation Alert"
        report_fl.source_description = "Brahmaputra basin water level rising"
        report_fl.source_severity = "Severe"
        report_fl.source_url = "https://sachet.ndma.gov.in"
        report_fl.data_source = MagicMock(name="NDMA SACHET")
        report_fl.issued_at = now
        report_fl.valid_until = now + timedelta(hours=6)

        with patch("app.services.alert_service.send_expo_push_notification") as mock_send_push:
            mock_send_push.return_value = (True, "DELIVERED", {"status": "ok"})
            dispatched = dispatch_alert_notifications_for_report(
                session=mock_session,
                report=report_fl,
                event=event_fl,
                classification=AlertClassification.OFFICIAL_WARNING,
                source_kind=AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING,
            )

        # Only Device A should have received a notification
        self.assertEqual(dispatched, 1)
        mock_session.add.assert_called()
        added_record = mock_session.add.call_args[0][0]
        self.assertEqual(added_record.device_id, "device-a-subscribed")
        self.assertEqual(added_record.disaster_type, CrisisType.FLOOD)

    def test_notification_history_retrieval_and_read_status(self) -> None:
        """Verifies retrieving notification history for a device and marking items as read."""
        now = datetime.now(timezone.utc)
        mock_session = MagicMock()

        notif_item = MagicMock(spec=AlertNotificationRecord)
        notif_item.id = uuid.uuid4()
        notif_item.device_id = "device-test-123"
        notif_item.crisis_event_id = uuid.uuid4()
        notif_item.disaster_type = CrisisType.CYCLONE
        notif_item.alert_classification = AlertClassification.OFFICIAL_WARNING
        notif_item.alert_source_kind = AlertSourceKind.OFFICIAL_GOVERNMENT_WARNING
        notif_item.title = "IMD Cyclone Warning: Severe Gale Winds"
        notif_item.body = "Wind gusts exceeding 120 km/h"
        notif_item.original_severity = "Severe"
        notif_item.issuing_authority = "India Meteorological Department"
        notif_item.source_url = "https://reactjs.imd.gov.in"
        notif_item.deep_link_url = "disasterapp://events/cyclone-1"
        notif_item.delivery_status = "DELIVERED"
        notif_item.is_read = False
        notif_item.issued_at = now
        notif_item.expires_at = now + timedelta(hours=12)
        notif_item.created_at = now

        mock_session.scalars.return_value.all.return_value = [notif_item]
        mock_session.scalar.return_value = 1  # 1 unread

        resp = get_device_notification_history(session=mock_session, device_id="device-test-123")
        self.assertTrue(resp.success)
        self.assertEqual(resp.total_notifications, 1)
        self.assertEqual(resp.unread_count, 1)
        self.assertEqual(resp.notifications[0].title, "IMD Cyclone Warning: Severe Gale Winds")
        self.assertIn(ALERT_SAFETY_DISCLAIMER, resp.safety_disclaimer)

        # Mark read
        mock_session.scalars.return_value.first.return_value = notif_item
        success_read = mark_notification_read(session=mock_session, notification_id=notif_item.id)
        self.assertTrue(success_read)
        self.assertTrue(notif_item.is_read)
        mock_session.commit.assert_called()

    @patch("app.services.alert_service.sync_earthquakes_from_ncs")
    @patch("app.services.alert_service.sync_all_flood_sources")
    @patch("app.services.alert_service.sync_all_cyclone_sources")
    def test_sync_active_disaster_alerts_and_logging(
        self,
        mock_sync_cyclone: MagicMock,
        mock_sync_flood: MagicMock,
        mock_sync_ncs: MagicMock,
    ) -> None:
        """Verifies manual and scheduled sync orchestrator logs monitoring health and summary."""
        mock_session = MagicMock()

        # Mock adapter responses
        mock_sync_ncs.return_value = MagicMock(created=2, updated=1)
        mock_sync_flood.return_value = MagicMock(total_created=1, total_updated=0)
        mock_sync_cyclone.return_value = MagicMock(total_created=0, total_updated=1)

        # No recent reports during this mock test
        mock_session.scalars.return_value.all.return_value = []

        resp = sync_active_disaster_alerts(session=mock_session, sync_mode="MANUAL")
        self.assertTrue(resp.success)
        self.assertEqual(resp.total_events_detected, 5)
        self.assertEqual(resp.monitoring_health, "SUCCESS")
        self.assertIn("NCS_SEISMO", resp.sources_synced)
        self.assertIn("FLOOD_SOURCES", resp.sources_synced)
        self.assertIn("CYCLONE_SOURCES", resp.sources_synced)
        mock_session.add.assert_called()  # Added AlertSyncLog
        mock_session.commit.assert_called()


if __name__ == "__main__":
    unittest.main()
