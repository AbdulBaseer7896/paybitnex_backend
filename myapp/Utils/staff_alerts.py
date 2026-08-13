"""
Internal staff notifications — "something is waiting in your queue".

Staff accounts are not recipients by default. Alerts are sent only when a
dedicated shared inbox is explicitly configured through
``STAFF_ALERT_EMAILS``. Customer-facing mail remains addressed to the customer
alone, so staff addresses are never exposed.

The configured shared inbox receives admin-portal links. Call sites pass a
portal-relative path and this module prefixes it with the admin portal root.

Both sync and async call sites exist — payments live on a normal ViewSet,
KYC on AsyncAPIView — hence the `anotify_staff` variant. The recipient lookup
is a DB query and would raise SynchronousOnlyOperation from async code.
"""
import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from myapp.Models.Auth_models import UserRole
from myapp.Utils.email_tasks import send_email_async

log = logging.getLogger(__name__)

# Portal root per role. Paths passed in are relative to these.
PORTAL_ROOT = {
    UserRole.ADMIN:      "/admin",
    UserRole.ACCOUNTANT: "/accountant",
}


def _recipients_by_role():
    """Return only explicitly configured staff-alert recipients.

    Admin and accountant account emails are never included implicitly. A
    deployment may opt in with a dedicated shared operations inbox through
    ``STAFF_ALERT_EMAILS``.
    """
    extra = [
        e.strip()
        for e in (getattr(settings, "STAFF_ALERT_EMAILS", "") or "").split(",")
        if e.strip()
    ]
    return {UserRole.ADMIN: extra} if extra else {}


def _send(by_role, *, subject, template, context, path, reply_to):
    """Fan the message out, one send per role group. No DB access."""
    if not by_role:
        log.warning(
            "staff alert %r has no recipients — is any admin/accountant "
            "active and holding an email address?", subject,
        )
        return

    frontend = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    for role, addrs in by_role.items():
        root = PORTAL_ROOT.get(role, PORTAL_ROOT[UserRole.ADMIN])
        send_email_async(
            to=sorted(set(addrs)),
            subject=subject,
            template=template,
            context={**context, "review_url": f"{frontend}{root}{path}"},
            reply_to=list(reply_to) if reply_to else None,
        )


def notify_staff(*, subject, template, context, path="", reply_to=None):
    """Alert staff about a queue item. Never raises — the originating
    action must succeed even when mail is misconfigured.

    path: portal-relative, e.g. "/transactions/<id>" or "/kyc".
    """
    try:
        _send(
            _recipients_by_role(), subject=subject, template=template,
            context=context, path=path, reply_to=reply_to,
        )
    except Exception:
        log.exception("staff alert failed: %r", subject)


async def anotify_staff(*, subject, template, context, path="", reply_to=None):
    """`notify_staff` for AsyncAPIView call sites."""
    try:
        by_role = await sync_to_async(_recipients_by_role, thread_sensitive=True)()
        _send(
            by_role, subject=subject, template=template,
            context=context, path=path, reply_to=reply_to,
        )
    except Exception:
        log.exception("staff alert failed: %r", subject)
