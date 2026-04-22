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
    count = qs.update(is_stale=True)
    logger.info(
        "flag_stale_payments: flagged %s PKR-sent payments (threshold=%s min)",
        count, minutes,
    )
    return {"flagged": count, "threshold_minutes": minutes}
