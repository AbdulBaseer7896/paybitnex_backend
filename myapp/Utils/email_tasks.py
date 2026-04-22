"""
Email delivery — all outbound email funnels through `send_email_async`,
which queues a Celery task. This keeps SMTP latency off the request
path and gives us retries on transient failures.

Usage:
    from myapp.Utils.email_tasks import send_email_async

    send_email_async(
        to=["user@example.com"],
        subject="Your OTP code",
        template="auth/otp",     # renders auth/otp.html + auth/otp.txt
        context={"code": "123456", "name": "Ali"},
        cc=[],                    # optional
        bcc=[],                   # optional
    )

Privacy: callers are responsible for NOT passing customer emails alongside
admin/accountant emails in the same `to` or `cc` — that would leak staff
addresses to customers. When you need to notify both audiences about the
same event, call `send_email_async` twice with different payloads.
"""
import logging
from typing import Dict, Iterable, Optional

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.template import TemplateDoesNotExist

log = logging.getLogger(__name__)


def _format_from() -> str:
    """Render 'PayBitnex <abc@gmail.com>' style From header."""
    name = getattr(settings, "EMAIL_FROM_NAME", "") or "PayBitnex"
    addr = settings.DEFAULT_FROM_EMAIL
    return f"{name} <{addr}>" if name and addr else addr


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,      # exponential: 1s, 2s, 4s, ...
    retry_backoff_max=60,    # cap at 60s between retries
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def send_email_task(self, *, to, subject, body_text, body_html=None,
                    cc=None, bcc=None, reply_to=None, attachments=None):
    """
    Low-level Celery task. Prefer `send_email_async` (which renders
    templates and dispatches here).

    `attachments` — optional list of dicts, each {"filename": str,
    "content_b64": str, "mimetype": str}. We base64-encode on the
    producer side so the attachment crosses the Celery broker as
    plain JSON; decoding happens here.
    """
    if not to:
        log.warning("send_email_task called with empty recipient list")
        return

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body_text,
            from_email=_format_from(),
            to=list(to),
            cc=list(cc) if cc else None,
            bcc=list(bcc) if bcc else None,
            reply_to=list(reply_to) if reply_to else None,
        )
        if body_html:
            msg.attach_alternative(body_html, "text/html")

        # Attach files (decode base64 on the worker side).
        if attachments:
            import base64
            for att in attachments:
                try:
                    content = base64.b64decode(att["content_b64"])
                    msg.attach(
                        att.get("filename", "attachment"),
                        content,
                        att.get("mimetype", "application/octet-stream"),
                    )
                except Exception as e:
                    log.warning("skipping bad attachment %r: %s",
                                att.get("filename"), e)

        sent = msg.send(fail_silently=False)
        log.info(
            "email sent: subject=%r to=%s cc=%s sent=%s attachments=%s",
            subject, to, cc, sent,
            [a.get("filename") for a in (attachments or [])],
        )
        return sent
    except Exception as exc:
        log.exception("email send failed: subject=%r to=%s error=%s",
                      subject, to, exc)
        # Re-raise so Celery's autoretry_for triggers.
        raise


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
    Queue an email for background delivery. Templates live under
    `myapp/templates/emails/<template>.html` (and optional `.txt`).

    If the `.txt` template is missing, a minimal plain-text fallback is
    auto-generated from the HTML (just the subject as the body) — this
    avoids blowing up if a template author forgets the text version.
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
        # Ultra-minimal plain-text fallback
        body_text = (
            f"{subject}\n\n"
            f"This message contains formatted HTML. If you can't see it, "
            f"please view the message in a client that supports HTML email."
        )

    # Filter Nones and dedupe while preserving order
    def _clean(seq):
        if not seq:
            return []
        seen = set()
        out = []
        for x in seq:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    send_email_task.delay(
        to=_clean(to),
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=_clean(cc),
        bcc=_clean(bcc),
        reply_to=_clean(reply_to),
        attachments=attachments or [],
    )
