"""
Firebase Cloud Messaging (FCM) Service for OS-level Push Notifications.

Delivers push notifications to Android (via Google Play Services) and iOS (via APNs)
even when the app process has been killed / swiped away by the user.

Configuration (.env):
    FIREBASE_CREDENTIALS_PATH = /path/to/firebase-service-account.json
    # OR
    FIREBASE_CREDENTIALS_JSON = {"type": "service_account", ...}
"""
import json
import logging
import os
from django.conf import settings

logger = logging.getLogger(__name__)

_firebase_initialized = False


def _get_firebase_app():
    """Lazily initialize the Firebase Admin App."""
    global _firebase_initialized
    if _firebase_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        # Already initialized by another module
        if firebase_admin._apps:
            _firebase_initialized = True
            return True

        cred_path = getattr(
            settings,
            "FIREBASE_CREDENTIALS_PATH",
            os.environ.get("FIREBASE_CREDENTIALS_PATH", ""),
        )
        cred_json = getattr(
            settings,
            "FIREBASE_CREDENTIALS_JSON",
            os.environ.get("FIREBASE_CREDENTIALS_JSON", ""),
        )

        cred = None
        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            logger.info("Initialized Firebase Admin using credentials file: %s", cred_path)
        elif cred_json:
            cert_dict = json.loads(cred_json) if isinstance(cred_json, str) else cred_json
            cred = credentials.Certificate(cert_dict)
            logger.info("Initialized Firebase Admin using JSON string.")

        if cred:
            firebase_admin.initialize_app(cred)
            _firebase_initialized = True
            return True
        else:
            logger.debug("Firebase credentials not configured. FCM push is disabled.")
            return False
    except Exception as exc:
        logger.warning("Could not initialize Firebase Admin SDK: %s", exc)
        return False


def send_fcm_push_to_user(user, title: str, message: str, data: dict = None):
    """
    Send an OS-level push notification to all active devices of a user.

    Ensures lock-screen notification delivery even if the app process
    is terminated / killed.
    """
    if not user:
        return 0

    from myapp.notifications.models import DeviceToken

    tokens = list(
        DeviceToken.objects.filter(user=user, is_active=True).values_list("token", flat=True)
    )
    if not tokens:
        logger.debug("No active FCM tokens for user %s", user.pk)
        return 0

    return send_fcm_push_to_tokens(tokens, title, message, data)


def send_fcm_push_to_users(users, title: str, message: str, data: dict = None):
    """Send push notification to multiple users (e.g. all admins)."""
    if not users:
        return 0

    from myapp.notifications.models import DeviceToken

    tokens = list(
        DeviceToken.objects.filter(user__in=users, is_active=True).values_list("token", flat=True)
    )
    if not tokens:
        return 0

    return send_fcm_push_to_tokens(tokens, title, message, data)


def send_fcm_push_to_tokens(tokens: list[str], title: str, message: str, data: dict = None):
    """Direct multicast dispatch with 500-token batching and stale token cleanup."""
    if not tokens:
        return 0

    if not _get_firebase_app():
        logger.debug("Skipping FCM send: Firebase is not configured.")
        return 0

    try:
        from firebase_admin import messaging
        from myapp.notifications.models import DeviceToken

        # FCM requires string-string dictionary in 'data'
        string_data = {}
        if data and isinstance(data, dict):
            for k, v in data.items():
                if v is not None:
                    string_data[str(k)] = str(v)

        # Android & iOS payload configuration for maximum wake-up reliability
        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                sound="default",
                channel_id="paidix_alerts",
                priority="high",
                click_action="FLUTTER_NOTIFICATION_CLICK",
            ),
        )
        apns_config = messaging.ApnsConfig(
            headers={"apns-priority": "10"},
            payload=messaging.ApnsPayload(
                aps=messaging.Aps(
                    sound="default",
                    content_available=True,
                    badge=1,
                )
            ),
        )

        total_success = 0
        stale_tokens = []

        # Firebase hard limit: 500 tokens per send_each_for_multicast call
        for i in range(0, len(tokens), 500):
            batch_tokens = tokens[i : i + 500]
            multicast_msg = messaging.MulticastMessage(
                tokens=batch_tokens,
                notification=messaging.Notification(title=title, body=message),
                data=string_data,
                android=android_config,
                apns=apns_config,
            )

            response = messaging.send_each_for_multicast(multicast_msg)
            total_success += response.success_count
            logger.info(
                "FCM multicast batch: %d success, %d failure of %d tokens",
                response.success_count,
                response.failure_count,
                len(batch_tokens),
            )

            # Handle deactivated or invalid tokens in this batch
            if response.failure_count > 0:
                for idx, resp in enumerate(response.responses):
                    if not resp.success and resp.exception:
                        exc_code = getattr(resp.exception, "code", "")
                        if (
                            "registration-token-not-registered" in str(resp.exception)
                            or exc_code == "UNREGISTERED"
                        ):
                            stale_tokens.append(batch_tokens[idx])

        if stale_tokens:
            logger.info("Deactivating %d stale FCM tokens", len(stale_tokens))
            DeviceToken.objects.filter(token__in=stale_tokens).update(is_active=False)

        return total_success
    except Exception as exc:
        logger.error("Failed to send FCM push notification: %s", exc, exc_info=True)
        return 0
