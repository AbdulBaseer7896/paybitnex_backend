"""
Report aggregation — generates DailyReport, then rolls up into
WeeklyReport and MonthlyReport. Exposed aggregate helpers are used
live by the reports API for quarterly / yearly / custom ranges.
"""
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from celery import shared_task
from django.db.models import Count, Q, Sum
from django.utils import timezone

log = logging.getLogger(__name__)


def _aggregate_range(start_date: date, end_date: date) -> dict:
    """
    Compute totals for payments created between [start_date, end_date] (inclusive).
    Returns a dict ready to feed into any _ReportBase subclass.
    """
    from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus

    start_dt = timezone.make_aware(
        timezone.datetime.combine(start_date, timezone.datetime.min.time())
    )
    end_dt = timezone.make_aware(
        timezone.datetime.combine(end_date, timezone.datetime.max.time())
    )

    qs = IncomingPayment.objects.filter(
        created_at__gte=start_dt, created_at__lte=end_dt,
    )

    counts = qs.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(status__in=[TransactionStatus.COMPLETED, TransactionStatus.PKR_SENT])),
        rejected=Count("id", filter=Q(status=TransactionStatus.REJECTED)),
    )

    by_currency_received = defaultdict(lambda: Decimal("0"))
    by_currency_fee = defaultdict(lambda: Decimal("0"))
    total_pkr_sent = Decimal("0")
    total_fee_pkr = Decimal("0")

    for row in qs.values("currency_id").annotate(
        total_amount=Sum("amount"),
        total_fee=Sum("fee_amount_foreign"),
        total_pkr=Sum("net_pkr"),
        total_fee_pkr_agg=Sum(
            "fee_amount_foreign",  # will be multiplied by rate below
        ),
    ):
        code = row["currency_id"]
        if row["total_amount"] is not None:
            by_currency_received[code] += row["total_amount"]
        if row["total_fee"] is not None:
            by_currency_fee[code] += row["total_fee"]
        if row["total_pkr"] is not None:
            total_pkr_sent += row["total_pkr"]

    # Fee-in-PKR: aggregate per row to honour per-transaction rates
    for p in qs.only("fee_amount_foreign", "exchange_rate"):
        if p.fee_amount_foreign and p.exchange_rate:
            total_fee_pkr += (p.fee_amount_foreign * p.exchange_rate)

    return {
        "period_start": start_date,
        "period_end": end_date,
        "total_transactions": counts["total"] or 0,
        "completed_transactions": counts["completed"] or 0,
        "rejected_transactions": counts["rejected"] or 0,
        "received_by_currency": {k: str(v) for k, v in by_currency_received.items()},
        "fees_by_currency": {k: str(v) for k, v in by_currency_fee.items()},
        "total_pkr_sent": total_pkr_sent,
        "total_fee_pkr": total_fee_pkr.quantize(Decimal("0.01")),
    }


# def generate_daily_report(self, for_date: str | None = None):
from typing import Optional

@shared_task(bind=True)
def generate_daily_report(self, for_date: Optional[str] = None):
    """Generate yesterday's daily report (or a specific ISO date)."""
    from myapp.Models.Report_models import DailyReport

    if for_date:
        d = date.fromisoformat(for_date)
    else:
        d = (timezone.now() - timedelta(days=1)).date()

    data = _aggregate_range(d, d)
    data["date"] = d
    obj, created = DailyReport.objects.update_or_create(
        date=d, defaults=data,
    )
    log.info("DailyReport %s — %s", "created" if created else "updated", d)
    return {"date": d.isoformat(), "created": created}


@shared_task
def generate_weekly_report(year: int, week: int):
    from myapp.Models.Report_models import WeeklyReport
    # ISO week → monday & sunday
    monday = date.fromisocalendar(year, week, 1)
    sunday = date.fromisocalendar(year, week, 7)
    data = _aggregate_range(monday, sunday)
    data.update(year=year, week=week)
    obj, created = WeeklyReport.objects.update_or_create(
        year=year, week=week, defaults=data,
    )
    return {"year": year, "week": week, "created": created}


@shared_task
def generate_monthly_report(year: int, month: int):
    from calendar import monthrange
    from myapp.Models.Report_models import MonthlyReport
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    data = _aggregate_range(first, last)
    data.update(year=year, month=month)
    obj, created = MonthlyReport.objects.update_or_create(
        year=year, month=month, defaults=data,
    )
    return {"year": year, "month": month, "created": created}
