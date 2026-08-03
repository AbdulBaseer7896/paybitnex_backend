"""
Celery task: flag PKR-sent payments as "stale" if they've been awaiting
customer confirmation for longer than the configured threshold.

Threshold settings (checked in priority order):
  1. `stale_payment_minutes` — if present, used directly (minute precision).
  2. `stale_payment_days`    — legacy setting, multiplied by 1440 to get minutes.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


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


@shared_task(name="myapp.Utils.stale_payment_tasks.flag_stale_payments")
def flag_stale_payments():
    from myapp.Models.Transaction_models import (
        IncomingPayment, TransactionStatus,
    )

    minutes = _resolve_threshold_minutes()
    cutoff = timezone.now() - timedelta(minutes=minutes)

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
    count = qs.update(is_stale=True)

    if newly_stale:
        _remind_customers_to_confirm(newly_stale)

    logger.info(
        "flag_stale_payments: flagged %s PKR-sent payments (threshold=%s min)",
        count, minutes,
    )
    return {"flagged": count, "threshold_minutes": minutes}


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
                },
            )
        except Exception:
            logger.exception(
                "stale confirm reminder failed for payment %s", payment.reference,
            )
