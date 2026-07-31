"""
Celery task: one daily summary of everything sitting in the queues.

This is the counterweight to the per-event staff alerts. Those fire the
moment something lands and are easy to lose in a busy inbox; this arrives
once a morning and answers "what is outstanding right now", including the
items that have been outstanding for a while and stopped generating noise.

Deliberately counts only — no per-row detail. A digest that lists every
pending payment becomes unreadable the week you get busy, which is exactly
the week you need it.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.db.models import Count, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name="myapp.Utils.digest_tasks.send_daily_ops_digest")
def send_daily_ops_digest():
    from myapp.Models.Invoicing_models import Invoice, InvoiceStatus
    from myapp.Models.Profile_models import CustomerProfile
    from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
    from myapp.Utils.staff_alerts import notify_staff

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    payments = IncomingPayment.objects.aggregate(
        awaiting_review=Count("pk", filter=Q(status__in=[
            TransactionStatus.SUBMITTED, TransactionStatus.UNDER_REVIEW,
        ])),
        verified_awaiting_transfer=Count("pk", filter=Q(
            status=TransactionStatus.VERIFIED,
        )),
        awaiting_confirmation=Count("pk", filter=Q(
            status=TransactionStatus.PKR_SENT,
        )),
        stale=Count("pk", filter=Q(
            status=TransactionStatus.PKR_SENT, is_stale=True,
        )),
        on_hold=Count("pk", filter=Q(status=TransactionStatus.ON_HOLD)),
    )

    # Yesterday's closes, by completed_at rather than occurred_on — this is
    # "what did we settle", not "what business date did it belong to".
    closed = IncomingPayment.objects.filter(
        status=TransactionStatus.COMPLETED,
        completed_at__date=yesterday,
    ).aggregate(count=Count("pk"), volume=Sum("amount"))

    kyc = CustomerProfile.objects.aggregate(
        pending=Count("pk", filter=Q(kyc_status=CustomerProfile.KYC_PENDING)),
        resubmitted=Count("pk", filter=Q(
            kyc_status=CustomerProfile.KYC_RESUBMITTED,
        )),
        objections=Count("pk", filter=Q(
            kyc_status=CustomerProfile.KYC_OBJECTIONS,
        )),
    )

    invoices_overdue = Invoice.objects.filter(
        status__in=[InvoiceStatus.SENT, InvoiceStatus.VIEWED],
        due_date__lt=today,
    ).count()

    kyc_waiting = kyc["pending"] + kyc["resubmitted"]
    total_open = (
        payments["awaiting_review"]
        + payments["verified_awaiting_transfer"]
        + payments["awaiting_confirmation"]
        + payments["on_hold"]
        + kyc_waiting
    )

    # A digest that says "nothing to do" every morning trains people to stop
    # opening it, so stay quiet on genuinely empty days.
    if total_open == 0 and invoices_overdue == 0 and not closed["count"]:
        logger.info("send_daily_ops_digest: nothing outstanding — skipped")
        return {"skipped": True}

    notify_staff(
        subject=f"PaidiX daily digest — {total_open} item(s) open",
        template="staff/daily_digest",
        context={
            "date":                today.isoformat(),
            "payments":            payments,
            "kyc":                 kyc,
            "kyc_waiting":         kyc_waiting,
            "invoices_overdue":    invoices_overdue,
            "closed_count":        closed["count"] or 0,
            "closed_volume":       f"{closed['volume'] or Decimal('0'):,.2f}",
            "total_open":          total_open,
        },
        path="",
    )

    logger.info(
        "send_daily_ops_digest: %s open, %s overdue invoices, %s closed "
        "yesterday", total_open, invoices_overdue, closed["count"] or 0,
    )
    return {
        "total_open": total_open,
        "invoices_overdue": invoices_overdue,
        "closed_yesterday": closed["count"] or 0,
    }