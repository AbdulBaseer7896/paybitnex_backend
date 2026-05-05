"""
Email delivery using aiosmtplib.

Outbound email funnels through ``send_email_async``, which renders the
configured Django templates and dispatches the actual SMTP delivery on
a background thread so the request thread never blocks on the network.

The previous implementation enqueued a Celery task and required a
worker to be running. That worked in production but made local /
preview deployments silently swallow OTPs whenever Celery wasn't up,
which is the bug we're now fixing. This implementation talks SMTP
directly via ``aiosmtplib`` using credentials read from the .env file:

    EMAIL_HOST=mail.bitnexpayout.com
    EMAIL_PORT=465
    EMAIL_HOST_USER=info@bitnexpayout.com
    EMAIL_HOST_PASSWORD=<password>
    EMAIL_USE_SSL=True
    DEFAULT_FROM_EMAIL=info@bitnexpayout.com
    EMAIL_FROM_NAME=PayBitnex

Usage (unchanged from before — every existing caller works):

    from myapp.Utils.email_tasks import send_email_async

    send_email_async(
        to=["user@example.com"],
        subject="Your OTP code",
        template="auth/otp_signup",
        context={"code": "123456", "name": "Ali"},
        cc=[],
        bcc=[],
    )

Privacy: callers are responsible for NOT mixing customer + staff
recipients in the same ``to`` / ``cc``. Call twice with different
payloads instead.

Failure handling: SMTP errors are logged but never propagated to the
HTTP request — the user sees their normal success response, and we
record the failure server-side. For OTP flows that's intentional: we
don't want to leak SMTP/recipient details back to the caller.
"""
import asyncio
import logging
import sys
import threading
from email.message import EmailMessage
from typing import Dict, Iterable, List, Optional

import aiosmtplib

from django.conf import settings
from django.template.loader import render_to_string
from django.template import TemplateDoesNotExist

log = logging.getLogger(__name__)


def _format_from() -> str:
    """Render 'PayBitnex <abc@bitnexpayout.com>' style From header."""
    name = getattr(settings, "EMAIL_FROM_NAME", "") or "PayBitnex"
    addr = settings.DEFAULT_FROM_EMAIL
    return f"{name} <{addr}>" if name and addr else addr


def _clean(seq) -> List[str]:
    """Filter Nones / empties and dedupe while preserving order."""
    if not seq:
        return []
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------
# Low-level SMTP delivery
# ---------------------------------------------------------------------

async def _send_via_aiosmtplib(
    *,
    to: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str],
    cc: List[str],
    bcc: List[str],
    reply_to: List[str],
    attachments: Optional[list],
) -> None:
    """Compose and send the message using aiosmtplib.

    Reads SMTP host / port / credentials / TLS mode from Django
    settings (which in turn read .env). Caller's responsibility to
    pass cleaned recipient lists.
    """
    msg = EmailMessage()
    msg["From"] = _format_from()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg["Subject"] = subject

    # Plain-text body always; HTML alternative when present. Setting
    # the text content first and then add_alternative preserves the
    # multipart/alternative structure mail clients expect.
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    # Attachments — base64-decode (callers send bytes pre-encoded so
    # the dispatch payload stays JSON-friendly) and add to the message.
    if attachments:
        import base64
        for att in attachments:
            try:
                content = base64.b64decode(att["content_b64"])
                maintype, _, subtype = att.get(
                    "mimetype", "application/octet-stream",
                ).partition("/")
                msg.add_attachment(
                    content,
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=att.get("filename", "attachment"),
                )
            except Exception as e:
                log.warning("skipping bad attachment %r: %s",
                            att.get("filename"), e)

    # Resolve TLS mode. Two switches:
    #   - EMAIL_USE_SSL=True  → implicit TLS (port 465 typically)
    #   - EMAIL_USE_TLS=True  → opportunistic STARTTLS (port 587 typically)
    # If neither is set we send in plaintext (only ever appropriate for
    # localhost / dev). aiosmtplib.send signature differs slightly here:
    # `use_tls=True` for implicit TLS, `start_tls=True` for STARTTLS.
    use_implicit_tls = bool(getattr(settings, "EMAIL_USE_SSL", False))
    use_starttls = bool(
        getattr(settings, "EMAIL_USE_TLS", False),
    ) and not use_implicit_tls

    host = settings.EMAIL_HOST
    port = settings.EMAIL_PORT
    username = settings.EMAIL_HOST_USER or None
    password = settings.EMAIL_HOST_PASSWORD or None
    timeout = getattr(settings, "EMAIL_TIMEOUT", 20) or 20

    # Build recipient list (To + Cc + Bcc). aiosmtplib handles RCPT TO
    # at the protocol level, so Bcc-only addresses are still delivered
    # but never appear in the rendered message — exactly what we want.
    recipients = list(to) + list(cc) + list(bcc)

    await aiosmtplib.send(
        msg,
        hostname=host,
        port=port,
        username=username,
        password=password,
        use_tls=use_implicit_tls,
        start_tls=use_starttls,
        timeout=timeout,
        recipients=recipients,
    )


def _send_via_django(
    *,
    to: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str],
    cc: List[str],
    bcc: List[str],
    reply_to: List[str],
) -> None:
    """Fallback path for the Django console / locmem backends.

    Used when EMAIL_BACKEND is something other than the SMTP backend —
    typically the console backend during local development. We still
    want OTPs to be visible somewhere (the dev console), and the
    Django backend prints them there.
    """
    from django.core.mail import EmailMultiAlternatives
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body_text,
        from_email=_format_from(),
        to=to,
        cc=cc or None,
        bcc=bcc or None,
        reply_to=reply_to or None,
    )
    if body_html:
        msg.attach_alternative(body_html, "text/html")
    msg.send(fail_silently=False)


def _dispatch_sync(payload: dict) -> None:
    """
    Run the SMTP send. Called from a worker thread (see
    ``send_email_async``) so blocking I/O is fine here.

    Decides between aiosmtplib and the Django backend based on the
    configured EMAIL_BACKEND. The aiosmtplib path is used for any
    real SMTP delivery; the Django path covers console / locmem /
    Anymail-Resend-via-API.
    """
    try:
        backend = getattr(settings, "EMAIL_BACKEND", "")
        # Use aiosmtplib for actual SMTP delivery. The Django SMTP
        # backend would also work, but aiosmtplib is what the user
        # specifically asked for and gives us cleaner async-mode
        # semantics + better diagnostics on TLS failures.
        if backend.endswith("smtp.EmailBackend") and settings.EMAIL_HOST:
            _run_async_send(payload)
        else:
            # Console / Anymail / locmem path. Drop attachments here —
            # those backends don't always support them, and the OTP
            # path doesn't use them.
            payload_no_att = {k: v for k, v in payload.items()
                              if k != "attachments"}
            _send_via_django(**payload_no_att)
        log.info(
            "email sent: subject=%r to=%s cc=%s",
            payload["subject"], payload["to"], payload["cc"],
        )
    except aiosmtplib.errors.SMTPAuthenticationError as e:
        # Surface a clear, actionable hint for the most common cause:
        # the SMTP credentials in settings are wrong / expired. Gmail
        # in particular requires an App Password (2FA must be enabled),
        # and silently rejects regular account passwords with a 535.
        # We keep the original traceback for full diagnostics, but
        # prepend a one-line summary so it's obvious at a glance.
        log.error(
            "email auth rejected by SMTP server (subject=%r to=%s): %s. "
            "Check EMAIL_HOST_USER / EMAIL_HOST_PASSWORD in settings — "
            "Gmail requires an App Password (Account → Security → App "
            "passwords) when 2-Step Verification is enabled.",
            payload.get("subject"), payload.get("to"), e,
        )
    except Exception:
        # Never raise back into the request thread — but keep a
        # full stack trace in the logs so we can diagnose later.
        log.exception(
            "email send failed: subject=%r to=%s",
            payload.get("subject"), payload.get("to"),
        )


def _run_async_send(payload: dict) -> None:
    """
    Drive the aiosmtplib coroutine on a dedicated event loop and
    tear it down cleanly.

    Why not just `asyncio.run`? On Windows the default
    ProactorEventLoop sometimes fires `_ProactorBasePipeTransport.__del__`
    AFTER the loop has been closed, raising a noisy
    "RuntimeError: Event loop is closed" in the GC pass. Building
    the loop ourselves and shutting down async generators / closing
    transports before close() avoids that. On non-Windows platforms
    this is functionally identical to `asyncio.run`.
    """
    # On Windows, prefer the SelectorEventLoop for our outbound SMTP —
    # it doesn't have the proactor's __del__-after-close bug and is
    # plenty fast for the ~kilobyte payload we're shipping.
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_send_via_aiosmtplib(**payload))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------

def send_email_async(
    *,
    to: Iterable[str],
    subject: str,
    template: str,
    context: Optional[Dict] = None,
    cc: Optional[Iterable[str]] = None,
    bcc: Optional[Iterable[str]] = None,
    reply_to: Optional[Iterable[str]] = None,
    attachments: Optional[list] = None,
):
    """
    Render the templates and send the email on a background thread.

    Templates live under ``myapp/templates/emails/<template>.html``
    (and an optional ``.txt`` fallback). If the .txt template is
    missing, a minimal plain-text body is auto-generated.

    Returns immediately — the actual SMTP round-trip happens off the
    request thread, so a slow mail server doesn't slow down the API.
    Errors are logged but never raised — call sites assume "fire and
    forget" semantics.
    """
    ctx = {
        "subject": subject,
        "frontend_url": getattr(settings, "FRONTEND_URL", ""),
        "company_name": getattr(settings, "EMAIL_FROM_NAME", "PayBitnex"),
        **(context or {}),
    }

    html_tpl = f"emails/{template}.html"
    txt_tpl  = f"emails/{template}.txt"

    body_html = render_to_string(html_tpl, ctx)
    try:
        body_text = render_to_string(txt_tpl, ctx)
    except TemplateDoesNotExist:
        # Ultra-minimal plain-text fallback so non-HTML-capable readers
        # see *something* meaningful. This branch is rarely hit because
        # most templates ship a paired .txt file.
        body_text = (
            f"{subject}\n\n"
            f"This message contains formatted HTML. If you can't see it, "
            f"please view the message in a client that supports HTML email."
        )

    payload = {
        "to":          _clean(to),
        "subject":     subject,
        "body_text":   body_text,
        "body_html":   body_html,
        "cc":          _clean(cc),
        "bcc":         _clean(bcc),
        "reply_to":    _clean(reply_to),
        "attachments": attachments or [],
    }

    if not payload["to"]:
        log.warning("send_email_async called with empty recipient list "
                    "(subject=%r) — nothing sent", subject)
        return

    # Dispatch on a daemon thread so the request returns immediately.
    # Daemon=True means the thread won't block process shutdown if the
    # SMTP server is hung. For OTP flows we WANT this — the user gets
    # their HTTP 200 instantly, and the email arrives a moment later.
    t = threading.Thread(
        target=_dispatch_sync,
        kwargs={"payload": payload},
        daemon=True,
        name="email-dispatch",
    )
    t.start()


# ---------------------------------------------------------------------
# Backwards compatibility — kept as a thin shim because some legacy
# call sites import ``send_email_task`` directly. We expose a no-op
# Celery-task-shaped function so imports still resolve, but it
# delegates to ``send_email_async``'s thread dispatcher.
# ---------------------------------------------------------------------

def _send_email_task_impl(*, to, subject, body_text, body_html=None,
                          cc=None, bcc=None, reply_to=None, attachments=None):
    """Compatibility helper — delegates straight to the thread dispatcher.

    Prefer ``send_email_async`` for new code. This function exists so
    any old code path that was importing ``send_email_task`` directly
    still resolves and behaves correctly.
    """
    payload = {
        "to":          _clean(to),
        "subject":     subject,
        "body_text":   body_text or "",
        "body_html":   body_html,
        "cc":          _clean(cc),
        "bcc":         _clean(bcc),
        "reply_to":    _clean(reply_to),
        "attachments": attachments or [],
    }
    if not payload["to"]:
        return
    t = threading.Thread(
        target=_dispatch_sync,
        kwargs={"payload": payload},
        daemon=True,
        name="email-dispatch",
    )
    t.start()


# Some old code did `send_email_task.delay(...)`. Mimic that API on
# this plain function so we don't have to grep + rewrite every call
# site at once.
class _CeleryShim:
    """Quacks like a Celery task: callable + has .delay(...).

    Lets pre-existing imports of ``send_email_task`` keep working
    whether they call it directly OR via ``.delay()``.
    """
    def __call__(self, *args, **kwargs):
        return _send_email_task_impl(*args, **kwargs)
    def delay(self, *args, **kwargs):
        return _send_email_task_impl(*args, **kwargs)


send_email_task = _CeleryShim()
