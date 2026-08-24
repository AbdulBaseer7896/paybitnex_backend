"""
Celery tasks driving the "Awaiting customer confirmation" queue.

Two stages, run by the same beat entry:

  1. `flag_stale_payments` — a payment sits in PKR_SENT waiting for the
     customer to click "I received my PKR". Once it has waited longer than
     `stale_payment_minutes`, we flag it `is_stale=True` and stamp
     `stale_at`, which surfaces it in the Awaiting Confirmation queue and
     sends the customer one reminder.

  2. `auto_confirm_stale_payments` — the customer is not required to
     respond forever. `auto_confirm_payment_minutes` (default 1440 = one
     day) after a payment went stale, we approve it on their behalf: an
     admin already verified the documents and sent the PKR, so a silent
     customer shouldn't hold the books open indefinitely. The payment
     completes and partner fees are distributed exactly as if the customer
     had confirmed by hand.

Threshold settings (checked in priority order):
  1. `stale_payment_minutes` — if present, used directly (minute precision).
  2. `stale_payment_days`    — legacy setting, multiplied by 1440 to get minutes.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction as dbtx
from django.utils import timezone

logger = logging.getLogger(__name__)

# Auto-approve one day after a payment enters the queue, unless the admin
# has configured otherwise. `0` disables auto-approval entirely and returns
# the queue to being drained by hand.
DEFAULT_AUTO_CONFIRM_MINUTES = 1440


def _resolve_threshold_minutes():
    from myapp.Models.Core_models import SystemSetting

    raw_min = SystemSetting.get("stale_payment_minutes", None)
    if raw_min not in (None, ""):
        try:
            n = int(str(raw_min).strip())
            if n >= 1:
                return n
        except (TypeError, ValueError):
            logger.warning(
                "stale threshold: bad stale_payment_minutes=%r, falling back", raw_min,
            )

    raw_days = SystemSetting.get("stale_payment_days", "3") or "3"
    try:
        days = max(1, int(str(raw_days).strip()))
    except (TypeError, ValueError):
        logger.warning(
            "stale threshold: bad stale_payment_days=%r, using default 3", raw_days,
        )
        days = 3
    return days * 1440


def _resolve_auto_confirm_minutes():
    """Minutes a payment may sit in the queue before we approve it for them.

    Returns 0 when auto-approval is switched off. A negative or unparseable
    value is treated as "misconfigured" and falls back to the default rather
    than to 0 — silently disabling auto-approval because someone typed a
    stray character would strand payments in the queue with no signal.
    """
    from myapp.Models.Core_models import SystemSetting

    raw = SystemSetting.get("auto_confirm_payment_minutes", None)
    if raw in (None, ""):
        return DEFAULT_AUTO_CONFIRM_MINUTES
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "auto-confirm: bad auto_confirm_payment_minutes=%r, using default %s",
            raw, DEFAULT_AUTO_CONFIRM_MINUTES,
        )
        return DEFAULT_AUTO_CONFIRM_MINUTES
    if n < 0:
        logger.warning(
            "auto-confirm: negative auto_confirm_payment_minutes=%r, using default %s",
            raw, DEFAULT_AUTO_CONFIRM_MINUTES,
        )
        return DEFAULT_AUTO_CONFIRM_MINUTES
    return n


@shared_task(name="myapp.Utils.stale_payment_tasks.flag_stale_payments")
def flag_stale_payments():
    from myapp.Models.Transaction_models import (
        IncomingPayment, TransactionStatus,
    )

    minutes = _resolve_threshold_minutes()
    now = timezone.now()
    cutoff = now - timedelta(minutes=minutes)

    qs = IncomingPayment.objects.filter(
        status=TransactionStatus.PKR_SENT,
        is_stale=False,
        updated_at__lt=cutoff,
    )
    # Capture before the update: after it, `is_stale=False` matches nothing
    # and the newly-flagged set is unrecoverable. This is also what keeps the
    # reminder to exactly one per staleness episode — the filter above only
    # ever sees a payment on the run that flips it, and customer_confirm /
    # force_complete are what clear the flag again.
    newly_stale = list(qs.values_list("id", flat=True))
    count = qs.update(is_stale=True, stale_at=now)

    if newly_stale:
        _remind_customers_to_confirm(newly_stale)

    logger.info(
        "flag_stale_payments: flagged %s PKR-sent payments (threshold=%s min)",
        count, minutes,
    )
    return {"flagged": count, "threshold_minutes": minutes}


@shared_task(name="myapp.Utils.stale_payment_tasks.auto_confirm_stale_payments")
def auto_confirm_stale_payments():
    """Approve payments the customer never got round to confirming.

    Runs the same transition `customer_confirm` does — COMPLETED, fee
    distribution, status history, audit log — with `auto_confirmed=True` so
    the audit trail says plainly that no human confirmed it.
    """
    from myapp.Models.Audit_models import AuditLog
    from myapp.Models.Transaction_models import (
        IncomingPayment, TransactionStatus,
    )
    from myapp.Utils.partner_ledger import distribute_fee_for_payment
    from myapp.Views.Transaction_views import _record_status_change

    minutes = _resolve_auto_confirm_minutes()
    if minutes == 0:
        logger.info("auto_confirm_stale_payments: disabled (0 minutes)")
        return {"confirmed": 0, "window_minutes": 0, "disabled": True}

    now = timezone.now()
    cutoff = now - timedelta(minutes=minutes)

    # `stale_at__isnull=False` matters: a payment flagged by an older build
    # has no anchor, and the 0061 migration backfills those. Anything still
    # NULL here is a payment flagged after the migration by a task that
    # somehow skipped the stamp — leave it for the next run rather than
    # guessing its age.
    due = list(
        IncomingPayment.objects
        .filter(
            status=TransactionStatus.PKR_SENT,
            is_stale=True,
            stale_at__isnull=False,
            stale_at__lt=cutoff,
        )
        .values_list("id", flat=True)
    )

    confirmed = 0
    for payment_id in due:
        try:
            with dbtx.atomic():
                # Re-read under a row lock: the customer may have confirmed,
                # or an admin force-completed, between building the list and
                # getting here. Double-completing would distribute partner
                # fees twice.
                payment = (
                    IncomingPayment.objects
                    .select_for_update()
                    .get(pk=payment_id)
                )
                if payment.status != TransactionStatus.PKR_SENT:
                    continue

                before_status = payment.status
                payment.customer_confirmed_at = now
                payment.completed_at = now
                payment.status = TransactionStatus.COMPLETED
                payment.is_stale = False
                payment.auto_confirmed = True
                payment.save(update_fields=[
                    "customer_confirmed_at", "completed_at", "status",
                    "is_stale", "auto_confirmed", "updated_at",
                ])
                _record_status_change(
                    payment, before_status, TransactionStatus.COMPLETED,
                    user=None,
                    note=(
                        "Auto-approved on the customer's behalf — no "
                        f"response within {minutes} minutes of the payment "
                        "entering Awaiting Customer Confirmation."
                    ),
                )
                distribute_fee_for_payment(payment)
                AuditLog.record(
                    user=None, action=AuditLog.ACTION_UPDATE,
                    target=payment,
                    description=(
                        f"{payment.reference}: auto-approved after "
                        f"{minutes} minutes in Awaiting Customer Confirmation"
                    ),
                )
            confirmed += 1
        except Exception:  # pragma: no cover — one bad row must not stop the rest
            logger.exception(
                "auto_confirm_stale_payments failed for payment %s", payment_id,
            )

    logger.info(
        "auto_confirm_stale_payments: confirmed %s of %s due (window=%s min)",
        confirmed, len(due), minutes,
    )
    return {"confirmed": confirmed, "due": len(due), "window_minutes": minutes}


def _remind_customers_to_confirm(payment_ids):
    """Nudge each customer to confirm receipt of a payment we've sent.

    Until they confirm, the payment can't complete and partner fees aren't
    distributed — so this unblocks our books as much as it reassures them.
    """
    from myapp.Models.Transaction_models import IncomingPayment
    from myapp.Utils.email_tasks import send_email_async

    payments = (
        IncomingPayment.objects
        .filter(id__in=payment_ids)
        .select_related("customer")
    )
    auto_minutes = _resolve_auto_confirm_minutes()
    for payment in payments:
        customer = getattr(payment, "customer", None)
        email = customer.email if customer and getattr(customer, "email", None) else ""
        if not email:
            continue
        try:
            send_email_async(
                to=[email],
                subject=f"Action needed: confirm receipt for {payment.reference}",
                template="payments/confirm_reminder",
                context={
                    "name":      payment.customer.full_name or "",
                    "reference": payment.reference,
                    "amount":    f"{payment.amount}",
                    "currency":  getattr(payment, "currency_id", None),
                    # Tell them the deadline rather than leaving the nudge
                    # open-ended — they should know it closes by itself.
                    "auto_confirm_minutes": auto_minutes,
                    "auto_confirm_hours": round(auto_minutes / 60) if auto_minutes else 0,
                },
            )
        except Exception:
            logger.exception(
                "stale confirm reminder failed for payment %s", payment.reference,
            )
