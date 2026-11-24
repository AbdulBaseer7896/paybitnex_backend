"""
Comprehensive tests for Django Channels WebSocket notifications,
Auth middleware, Notification service, and REST API endpoints.
"""
import json
from decimal import Decimal
from django.test import TestCase, override_settings
from django.utils import timezone
from django.db import transaction as dbtx
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from channels.testing import WebsocketCommunicator

from paybitnex.asgi import application
from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Core_models import Currency
from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
from myapp.notifications.models import Notification, NotificationType
from myapp.notifications.services import (
    notify_payment_status_updated,
    notify_payment_received_confirmed,
)


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class NotificationWebSocketConsumerTests(TestCase):
    """Consumer tests: auth success/failure, group subscriptions, ping/pong."""

    def setUp(self):
        self.customer = User.objects.create(
            email="cust1@example.com",
            role=UserRole.CUSTOMER,
            is_active=True,
            email_verified=True,
        )
        self.other_customer = User.objects.create(
            email="cust2@example.com",
            role=UserRole.CUSTOMER,
            is_active=True,
            email_verified=True,
        )
        self.admin = User.objects.create(
            email="admin@example.com",
            role=UserRole.ADMIN,
            is_active=True,
            email_verified=True,
        )
        self.customer_token = str(AccessToken.for_user(self.customer))
        self.other_customer_token = str(AccessToken.for_user(self.other_customer))
        self.admin_token = str(AccessToken.for_user(self.admin))

    async def test_auth_failure_no_token(self):
        """Reject connection if no token is provided (code 4401)."""
        comm = WebsocketCommunicator(application, "/ws/notifications/")
        connected, code = await comm.connect()
        self.assertTrue(not connected or code == 4401)
        await comm.disconnect()

    async def test_auth_failure_invalid_token(self):
        """Reject connection with invalid token (code 4401)."""
        comm = WebsocketCommunicator(application, "/ws/notifications/?token=invalid_garbage")
        connected, code = await comm.connect()
        self.assertTrue(not connected or code == 4401)
        await comm.disconnect()

    async def test_auth_success_and_ping_pong(self):
        """Authenticated connection succeeds and responds to ping with pong."""
        comm = WebsocketCommunicator(application, f"/ws/notifications/?token={self.customer_token}")
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_json_to({"type": "ping"})
        response = await comm.receive_json_from(timeout=2)
        self.assertEqual(response, {"type": "pong"})
        await comm.disconnect()

    async def test_customer_receives_own_event_only(self):
        """Customer receives their own event and NOT another customer's event."""
        comm_cust1 = WebsocketCommunicator(application, f"/ws/notifications/?token={self.customer_token}")
        comm_cust2 = WebsocketCommunicator(application, f"/ws/notifications/?token={self.other_customer_token}")

        self.assertTrue((await comm_cust1.connect())[0])
        self.assertTrue((await comm_cust2.connect())[0])

        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()

        # Send event to cust1 group
        payload = {
            "type": "payment_status_updated",
            "notification_id": 99,
            "message": "Payment verified for cust1",
        }
        await channel_layer.group_send(
            f"user_{self.customer.pk}",
            {"type": "notification.send", "payload": payload},
        )

        # Cust1 must receive
        received = await comm_cust1.receive_json_from(timeout=2)
        self.assertEqual(received["notification_id"], 99)

        # Cust2 must NOT receive
        self.assertTrue(await comm_cust2.receive_nothing(timeout=0.5))

        await comm_cust1.disconnect()
        await comm_cust2.disconnect()

    async def test_admin_receives_admin_event_and_customer_does_not(self):
        """Admins receive broadcast events; regular customers do not join admins group."""
        comm_cust = WebsocketCommunicator(application, f"/ws/notifications/?token={self.customer_token}")
        comm_admin = WebsocketCommunicator(application, f"/ws/notifications/?token={self.admin_token}")

        self.assertTrue((await comm_cust.connect())[0])
        self.assertTrue((await comm_admin.connect())[0])

        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()

        admin_payload = {
            "type": "payment_received_confirmed",
            "notification_id": 88,
            "message": "Customer confirmed payment",
        }
        await channel_layer.group_send(
            "admins",
            {"type": "notification.send", "payload": admin_payload},
        )

        # Admin receives
        received = await comm_admin.receive_json_from(timeout=2)
        self.assertEqual(received["notification_id"], 88)

        # Customer does not receive
        self.assertTrue(await comm_cust.receive_nothing(timeout=0.5))

        await comm_cust.disconnect()
        await comm_admin.disconnect()


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class NotificationServiceTests(TestCase):
    """Service tests: atomic rollback behavior and status-change guards."""

    def setUp(self):
        self.customer = User.objects.create(
            email="cust_srv@example.com",
            role=UserRole.CUSTOMER,
            is_active=True,
        )
        self.currency, _ = Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar", "is_active": True})
        self.payment = IncomingPayment.objects.create(
            customer=self.customer,
            reference="PBX-SRV-001",
            currency=self.currency,
            amount=Decimal("250.00"),
            sender_name="Alice",
            sender_company="Alice Corp",
            external_transaction_id="TX-SRV-001",
            status=TransactionStatus.SUBMITTED,
        )

    def test_no_notification_if_status_not_changed(self):
        """Service does not create notification if old_status == new_status."""
        notif = notify_payment_status_updated(
            payment=self.payment,
            old_status=TransactionStatus.SUBMITTED,
            new_status=TransactionStatus.SUBMITTED,
        )
        self.assertIsNone(notif)
        self.assertEqual(Notification.objects.count(), 0)

    def test_notification_rolls_back_on_transaction_failure(self):
        """Notification row and channel event do not persist on rolled back transaction."""
        try:
            with dbtx.atomic():
                notify_payment_status_updated(
                    payment=self.payment,
                    old_status=TransactionStatus.SUBMITTED,
                    new_status=TransactionStatus.UNDER_REVIEW,
                )
                # Force rollback
                raise RuntimeError("Simulated DB failure")
        except RuntimeError:
            pass

        self.assertEqual(Notification.objects.count(), 0)


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECURE_SSL_REDIRECT=False,
)
class NotificationRESTAPITests(TestCase):
    """REST API tests: list, unread-count, mark-read, read-all, isolation."""

    def setUp(self):
        self.client = APIClient()
        self.user1 = User.objects.create(
            email="user1@example.com",
            role=UserRole.CUSTOMER,
            is_active=True,
        )
        self.user2 = User.objects.create(
            email="user2@example.com",
            role=UserRole.CUSTOMER,
            is_active=True,
        )
        self.n1 = Notification.objects.create(
            recipient=self.user1,
            type=NotificationType.PAYMENT_STATUS_UPDATED,
            title="Update 1",
            message="Msg 1",
        )
        self.n2 = Notification.objects.create(
            recipient=self.user1,
            type=NotificationType.PAYMENT_STATUS_UPDATED,
            title="Update 2",
            message="Msg 2",
        )
        self.n3 = Notification.objects.create(
            recipient=self.user2,
            type=NotificationType.PAYMENT_STATUS_UPDATED,
            title="Update 3",
            message="Msg 3",
        )

    def test_list_notifications_scoped_to_user(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.get("/api/v1/notifications/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data.get("results", resp.data)
        self.assertEqual(len(results), 2)

    def test_unread_count(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.get("/api/v1/notifications/unread-count/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["unread_count"], 2)

    def test_mark_read(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.patch(f"/api/v1/notifications/{self.n1.id}/read/", follow=True)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["is_read"])

        # Check unread count dropped to 1
        resp_count = self.client.get("/api/v1/notifications/unread-count/", follow=True)
        self.assertEqual(resp_count.data["unread_count"], 1)

    def test_cannot_access_other_users_notification(self):
        """User 1 cannot mark User 2's notification as read (returns 404)."""
        self.client.force_authenticate(user=self.user1)
        resp = self.client.patch(f"/api/v1/notifications/{self.n3.id}/read/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_mark_read(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.patch(f"/api/v1/notifications/{self.n1.id}/read/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["is_read"])

        # Check unread count dropped to 1
        resp_count = self.client.get("/api/v1/notifications/unread-count/")
        self.assertEqual(resp_count.data["unread_count"], 1)

    def test_read_all(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.post("/api/v1/notifications/read-all/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["updated_count"], 2)

        resp_count = self.client.get("/api/v1/notifications/unread-count/")
        self.assertEqual(resp_count.data["unread_count"], 0)

        # User 2's notification should still be unread
        self.assertFalse(Notification.objects.get(id=self.n3.id).is_read)

    def test_device_token_registration_and_unregistration(self):
        """Test POST /api/v1/notifications/devices/ and unregister."""
        self.client.force_authenticate(user=self.user1)
        resp = self.client.post(
            "/api/v1/notifications/devices/",
            {"token": "fcm_test_token_12345", "device_type": "android"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        from myapp.notifications.models import DeviceToken
        self.assertTrue(DeviceToken.objects.filter(token="fcm_test_token_12345", user=self.user1, is_active=True).exists())

        # Unregister
        resp_unreg = self.client.post(
            "/api/v1/notifications/devices/unregister/",
            {"token": "fcm_test_token_12345"},
            format="json",
        )
        self.assertEqual(resp_unreg.status_code, status.HTTP_200_OK)
        self.assertFalse(DeviceToken.objects.get(token="fcm_test_token_12345").is_active)


class KYCNotificationTests(TestCase):
    """Tests for KYC status update and submission notification service."""

    def setUp(self):
        self.customer = User.objects.create(
            email="kyc_cust@example.com",
            role=UserRole.CUSTOMER,
            full_name="KYC Customer",
            is_active=True,
            email_verified=True,
        )
        self.admin = User.objects.create(
            email="kyc_admin@example.com",
            role=UserRole.ADMIN,
            is_active=True,
            email_verified=True,
        )
        from myapp.Models.Profile_models import CustomerProfile
        self.profile = CustomerProfile.objects.create(
            user=self.customer,
            full_name="KYC Customer",
            kyc_status="pending",
        )

    def test_notify_kyc_status_updated_approved(self):
        from myapp.notifications.services import notify_kyc_status_updated
        with dbtx.atomic():
            notif = notify_kyc_status_updated(self.profile, old_status="pending", new_status="approved")

        self.assertIsNotNone(notif)
        self.assertEqual(notif.recipient, self.customer)
        self.assertEqual(notif.type, NotificationType.KYC_STATUS_UPDATED)
        self.assertEqual(notif.title, "KYC Approved")
        self.assertEqual(notif.payload["status"], "approved")

    def test_notify_kyc_status_updated_objections(self):
        from myapp.notifications.services import notify_kyc_status_updated
        with dbtx.atomic():
            notif = notify_kyc_status_updated(
                self.profile,
                old_status="pending",
                new_status="objections",
                notes="Selfie is blurred",
            )

        self.assertIsNotNone(notif)
        self.assertEqual(notif.title, "KYC Objections Raised")
        self.assertIn("Selfie is blurred", notif.message)
        self.assertEqual(notif.payload["status"], "objections")

    def test_notify_kyc_submitted(self):
        from myapp.notifications.services import notify_kyc_submitted
        with dbtx.atomic():
            notif_id = notify_kyc_submitted(self.profile, is_resubmission=False)

        self.assertIsNotNone(notif_id)
        admin_notif = Notification.objects.filter(recipient=self.admin, type=NotificationType.KYC_SUBMITTED).first()
        self.assertIsNotNone(admin_notif)
        self.assertEqual(admin_notif.title, "KYC Submitted")
