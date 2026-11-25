"""
Notification service for PaidiX.

Core responsibilities:
  1. Creates and persists a Notification model instance in the database.
  2. Dispatches real-time WebSocket events to the channel layer using
     `transaction.on_commit()` so rollbacks never push events.
  3. Never raises an uncaught exception: channel-layer errors are logged
     and swallowed so core payment workflows never break.

Push notifications (FCM / APNs) note:
  This service is the exact plug-in point for background mobile push
  notifications when the app is closed or in the background. In the future,
  call your FCM task right after `async_to_sync(channel_layer.group_send)`.
"""
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction as dbtx
from django.utils import timezone

from myapp.notifications.models import Notification, NotificationType
from myapp.notifications.fcm import send_fcm_push_to_user, send_fcm_push_to_users

logger = logging.getLogger(__name__)

STATUS_TITLES = {
    "submitted": "Payment Submitted",
    "under_review": "Payment Under Review",
    "verified": "Payment Verified",
    "pkr_sent": "PKR Sent to Your Account",
    "completed": "Payment Completed",
    "on_hold": "Payment On Hold",
    "rejected": "Payment Rejected",
}


def _send_to_channel_layer(group_name: str, payload: dict):
    """Safe helper to send to a Channels group without raising."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            logger.warning("Notification channel layer is not configured.")
            return

        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "notification.send",
                "payload": payload,
            },
        )
    except Exception as exc:
        # Never break database operations or payments due to channel layer failure
        logger.error(
            "Failed to send notification to group '%s': %s",
            group_name,
            exc,
            exc_info=True,
        )


def notify_payment_status_updated(payment, old_status: str, new_status: str, note: str = "", is_auto_confirm: bool = False):
    """
    Notify customer when an admin, accountant, or system updates payment status.

    - Old status must differ from new status.
    - Saves Notification row in DB.
    - Uses transaction.on_commit() to push to `user_<customer_id>` group.
    """
    if old_status == new_status:
        return None

    customer = getattr(payment, "customer", None)
    if not customer:
        return None

    status_title = STATUS_TITLES.get(new_status, new_status.replace("_", " ").title())
    if is_auto_confirm:
        title = "Payment Auto-Confirmed"
        message = (
            f"Payment {payment.reference} was automatically confirmed and marked completed."
        )
    elif new_status == "pkr_sent":
        title = status_title
        message = f"PKR has been sent for payment {payment.reference}. Please verify in your bank account."
    elif new_status == "completed":
        title = status_title
        message = f"Payment {payment.reference} is now completed. Thank you!"
    else:
        title = status_title
        message = f"Status for payment {payment.reference} updated from {old_status} to {new_status}."

    if note:
        message += f" ({note})"

    now_iso = timezone.now().isoformat()

    # Pre-build payload contract for mobile
    payload = {
        "type": NotificationType.PAYMENT_STATUS_UPDATED,
        "event": NotificationType.PAYMENT_STATUS_UPDATED,
        "notification_id": None,  # Filled after DB save
        "payment_id": str(payment.id),
        "reference": getattr(payment, "reference", ""),
        "old_status": str(old_status or ""),
        "new_status": str(new_status),
        "status": str(new_status),
        "title": title,
        "message": message,
        "created_at": now_iso,
    }

    try:
        notification = Notification.objects.create(
            recipient=customer,
            type=NotificationType.PAYMENT_STATUS_UPDATED,
            payment=payment,
            title=title,
            message=message,
            payload=payload,
        )
        # Update payload with generated notification_id
        payload["notification_id"] = notification.id
        notification.payload = payload
        notification.save(update_fields=["payload"])

        # Push to customer's personal WebSocket group on transaction commit
        group_name = f"user_{customer.pk}"
        dbtx.on_commit(lambda: _send_to_channel_layer(group_name, payload))

        # Push to customer's device lock screen via FCM (delivered even when app is killed)
        dbtx.on_commit(lambda: send_fcm_push_to_user(customer, title, message, payload))

        return notification
    except Exception as exc:
        logger.error("Failed to create customer status notification: %s", exc, exc_info=True)
        return None


def notify_payment_submitted(payment):
    """
    Notify all admins/accountants when a customer submits a new payment.

    - Saves Notification for each active admin/accountant.
    - Sends to the 'admins' Channels group on commit.
    - Sends FCM push to admin/accountant devices on commit.
    """
    customer = getattr(payment, "customer", None)
    customer_id = str(customer.id) if customer else ""
    customer_name = (
        (getattr(customer, "full_name", "") or getattr(customer, "email", ""))
        if customer else "Customer"
    )

    title = "New Payment Submitted"
    message = f"{customer_name} submitted payment {payment.reference} ({payment.amount} {payment.currency_id}) for review."
    now_iso = timezone.now().isoformat()

    payload = {
        "type": NotificationType.PAYMENT_SUBMITTED,
        "event": NotificationType.PAYMENT_SUBMITTED,
        "notification_id": None,
        "payment_id": str(payment.id),
        "reference": getattr(payment, "reference", ""),
        "old_status": "",
        "new_status": str(getattr(payment, "status", "submitted")),
        "status": str(getattr(payment, "status", "submitted")),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "title": title,
        "message": message,
        "created_at": now_iso,
    }

    try:
        from myapp.Models.Auth_models import User, UserRole

        staff_users = list(
            User.objects.filter(role__in=[UserRole.ADMIN, UserRole.ACCOUNTANT], is_active=True)
        )

        notification_id = None
        for staff in staff_users:
            notif = Notification.objects.create(
                recipient=staff,
                type=NotificationType.PAYMENT_SUBMITTED,
                payment=payment,
                title=title,
                message=message,
                payload=payload,
            )
            if notification_id is None:
                notification_id = notif.id

        if notification_id:
            payload["notification_id"] = notification_id

        dbtx.on_commit(lambda: _send_to_channel_layer("admins", payload))
        dbtx.on_commit(lambda: send_fcm_push_to_users(staff_users, title, message, payload))
        return notification_id
    except Exception as exc:
        logger.error("Failed to create admin payment submitted notification: %s", exc, exc_info=True)
        return None


def notify_payment_received_confirmed(payment, old_status: str, new_status: str, confirmed_by_note: str = ""):
    """
    Notify all admins/accountants when a customer confirms PKR receipt.

    - Saves Notification for each admin/accountant or handles broadcast.
    - Sends to the 'admins' Channels group.
    - Sends FCM push to admin devices.
    """
    customer = getattr(payment, "customer", None)
    customer_id = str(customer.id) if customer else ""
    customer_name = (
        (getattr(customer, "full_name", "") or getattr(customer, "email", ""))
        if customer else "Customer"
    )

    title = "Customer Confirmed Receipt"
    message = f"{customer_name} confirmed receipt for payment {payment.reference}."
    if confirmed_by_note:
        message += f" Note: {confirmed_by_note}"

    now_iso = timezone.now().isoformat()

    payload = {
        "type": NotificationType.PAYMENT_RECEIVED_CONFIRMED,
        "event": NotificationType.PAYMENT_RECEIVED_CONFIRMED,
        "notification_id": None,
        "payment_id": str(payment.id),
        "reference": getattr(payment, "reference", ""),
        "old_status": str(old_status or ""),
        "new_status": str(new_status),
        "status": str(new_status),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "title": title,
        "message": message,
        "created_at": now_iso,
    }

    try:
        from myapp.Models.Auth_models import User, UserRole

        # Save notifications for admin and accountant recipients
        staff_users = list(
            User.objects.filter(role__in=[UserRole.ADMIN, UserRole.ACCOUNTANT], is_active=True)
        )

        notification_id = None
        for staff in staff_users:
            notif = Notification.objects.create(
                recipient=staff,
                type=NotificationType.PAYMENT_RECEIVED_CONFIRMED,
                payment=payment,
                title=title,
                message=message,
                payload=payload,
            )
            if notification_id is None:
                notification_id = notif.id

        if notification_id:
            payload["notification_id"] = notification_id

        # Broadcast to all connected admins via the 'admins' group on commit
        dbtx.on_commit(lambda: _send_to_channel_layer("admins", payload))

        # Push to admin device lock screens via FCM
        dbtx.on_commit(lambda: send_fcm_push_to_users(staff_users, title, message, payload))

        return notification_id
    except Exception as exc:
        logger.error("Failed to create admin payment confirmed notification: %s", exc, exc_info=True)
        return None


def notify_kyc_status_updated(profile, old_status: str, new_status: str, notes: str = ""):
    """
    Notify customer when their KYC verification status changes (Approved / Rejected / Objections).

    - Saves Notification in DB.
    - Sends real-time WebSocket event to customer's personal group.
    - Sends lock-screen push notification to customer's device via FCM.
    """
    if old_status == new_status:
        return None

    user = getattr(profile, "user", None)
    if not user:
        return None

    if new_status == "approved":
        title = "KYC Approved"
        message = "Your PaidiX account has been verified! You can now send and receive payments."
    elif new_status == "rejected":
        title = "KYC Verification Rejected"
        message = "Your PaidiX KYC verification was rejected."
        if notes:
            message += f" Reason: {notes}"
    elif new_status == "objections":
        title = "KYC Objections Raised"
        message = "Action required: Objections raised on your KYC documents. Please review and resubmit."
        if notes:
            message += f" Notes: {notes}"
    else:
        title = f"KYC Status: {new_status.replace('_', ' ').title()}"
        message = f"Your KYC verification status has been updated to {new_status}."

    now_iso = timezone.now().isoformat()

    payload = {
        "type": NotificationType.KYC_STATUS_UPDATED,
        "event": NotificationType.KYC_STATUS_UPDATED,
        "notification_id": None,
        "kyc_status": str(new_status),
        "status": str(new_status),
        "old_status": str(old_status or ""),
        "profile_id": str(getattr(profile, "id", "")),
        "title": title,
        "message": message,
        "notes": str(notes or ""),
        "created_at": now_iso,
    }

    try:
        notification = Notification.objects.create(
            recipient=user,
            type=NotificationType.KYC_STATUS_UPDATED,
            payment=None,
            title=title,
            message=message,
            payload=payload,
        )
        payload["notification_id"] = notification.id
        notification.payload = payload
        notification.save(update_fields=["payload"])

        # Push to customer's personal WebSocket group on transaction commit
        group_name = f"user_{user.pk}"
        dbtx.on_commit(lambda: _send_to_channel_layer(group_name, payload))

        # Push to customer's lock screen via FCM
        dbtx.on_commit(lambda: send_fcm_push_to_user(user, title, message, payload))

        return notification
    except Exception as exc:
        logger.error("Failed to create KYC status notification: %s", exc, exc_info=True)
        return None


def notify_kyc_submitted(profile, is_resubmission: bool = False):
    """
    Notify admins & accountants when a customer submits or resubmits KYC.

    - Saves Notification for staff.
    - Sends real-time WebSocket event to 'admins' Channels group.
    - Sends lock-screen push notification to staff devices via FCM.
    """
    user = getattr(profile, "user", None)
    customer_name = (
        (getattr(profile, "full_name", "") or getattr(user, "full_name", "") or getattr(user, "email", ""))
        if user else "Customer"
    )

    action_label = "resubmitted" if is_resubmission else "submitted"
    title = f"KYC {action_label.title()}"
    message = f"Customer {customer_name} {action_label} KYC documents for review."

    now_iso = timezone.now().isoformat()

    payload = {
        "type": NotificationType.KYC_SUBMITTED,
        "event": NotificationType.KYC_SUBMITTED,
        "notification_id": None,
        "kyc_status": str(getattr(profile, "kyc_status", "pending")),
        "status": str(getattr(profile, "kyc_status", "pending")),
        "profile_id": str(getattr(profile, "id", "")),
        "customer_id": str(user.id) if user else "",
        "customer_name": customer_name,
        "title": title,
        "message": message,
        "created_at": now_iso,
    }

    try:
        from myapp.Models.Auth_models import User, UserRole

        staff_users = list(
            User.objects.filter(role__in=[UserRole.ADMIN, UserRole.ACCOUNTANT], is_active=True)
        )

        notification_id = None
        for staff in staff_users:
            notif = Notification.objects.create(
                recipient=staff,
                type=NotificationType.KYC_SUBMITTED,
                payment=None,
                title=title,
                message=message,
                payload=payload,
            )
            if notification_id is None:
                notification_id = notif.id

        if notification_id:
            payload["notification_id"] = notification_id

        # Broadcast to admins WebSocket group
        dbtx.on_commit(lambda: _send_to_channel_layer("admins", payload))

        # Push to admin devices via FCM
        dbtx.on_commit(lambda: send_fcm_push_to_users(staff_users, title, message, payload))

        return notification_id
    except Exception as exc:
        logger.error("Failed to notify KYC submission: %s", exc, exc_info=True)
        return None
