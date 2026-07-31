"""
Internal staff notifications — "something is waiting in your queue".

Everything here goes to the people who work the queues (active admins and
accountants) and never to a customer. Customer-facing mail is sent from the
individual views, addressed to the customer alone, so staff addresses are
never exposed.

Recipients are resolved per role and sent one group per role. That grouping
isn't cosmetic: the two portals mount the same review screens under different
prefixes (`/admin/kyc` vs `/accountant/kyc`), so a single shared link would
404 for half the recipients. Call sites pass a portal-RELATIVE path and each
group gets it prefixed with their own root.

Both sync and async call sites exist — payments live on a normal ViewSet,
KYC on AsyncAPIView — hence the `anotify_staff` variant. The recipient lookup
is a DB query and would raise SynchronousOnlyOperation from async code.
"""
import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from myapp.Models.Auth_models import User, UserRole
from myapp.Utils.email_tasks import send_email_async

log = logging.getLogger(__name__)

# Staff roles that work the review queues.
ALERT_ROLES = (UserRole.ADMIN, UserRole.ACCOUNTANT)

# Portal root per role. Paths passed in are relative to these.
PORTAL_ROOT = {
    UserRole.ADMIN:      "/admin",
    UserRole.ACCOUNTANT: "/accountant",
}


def _recipients_by_role():
    """{role: [email, ...]} for staff who can act on a queue item."""
    by_role = {}
    for email, role in (
        User.objects
        .filter(role__in=ALERT_ROLES, is_active=True)
        .exclude(email="")
        .values_list("email", "role")
    ):
        by_role.setdefault(role, []).append(email)

    # A shared ops inbox that isn't a login. Given the admin-portal link
    # because it has no role of its own to resolve against.
    extra = [
        e.strip()
        for e in (getattr(settings, "STAFF_ALERT_EMAILS", "") or "").split(",")
        if e.strip()
    ]
    if extra:
        by_role.setdefault(UserRole.ADMIN, []).extend(extra)

    return by_role


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