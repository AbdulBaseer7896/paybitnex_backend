"""
Reports views — period closing and aggregations.

Primary endpoint: GET /reports/closing/
  Query params:
    period=day|week|month|year   (bucket size, default: month)
    date_from=YYYY-MM-DD          (required unless period=all_time)
    date_to=YYYY-MM-DD            (required unless period=all_time)
    currency=USD|EUR|GBP|all      (default: all)
    customer=<uuid>               (optional — restrict to one customer)
    partner=<uuid>                (optional — restrict to one partner)
    status=completed|all          (default: completed — profit only counted
                                    on money we've actually disbursed)

  Returns a single object with:
    - buckets: [{ period_start, period_label, ... totals ... }, ...]
      one row per date-bucket in the range
    - totals: grand totals across the whole range
    - customer_rollup: [{ customer, total_received, profit, tx_count }, ...]
    - partner_rollup: [{ partner, profit_share_pct, total_pkr, tx_count }, ...]
    - expense_totals: totals across expenses in the same range
    - filters: echo of the filters applied (for the CSV header, etc.)

Secondary endpoint: GET /reports/closing.csv
  Same query params — returns a downloadable CSV with the buckets table.
"""
import csv
from decimal import Decimal
from datetime import date, datetime, timedelta
from io import StringIO

from django.db.models import Sum, Count, F, DecimalField, Q
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth, TruncYear
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
from myapp.Models.Partner_models import Partner, PartnerLedgerEntry, PartnerShare
from myapp.Models.Expense_models import Expense
from myapp.Utils.permissions import IsAdmin


PERIOD_TRUNCS = {
    "day":   TruncDay,
    "week":  TruncWeek,
    "month": TruncMonth,
    "year":  TruncYear,
}


def _parse_date(value):
    """YYYY-MM-DD → date. Returns None if the value is blank or invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_period_label(period, dt):
    """Human-friendly bucket label."""
    if period == "day":
        return dt.strftime("%Y-%m-%d")
    if period == "week":
        return f"Week of {dt.strftime('%Y-%m-%d')}"
    if period == "month":
        return dt.strftime("%B %Y")
    if period == "year":
        return dt.strftime("%Y")
    return str(dt)


def _build_filters(request):
    """Parse and validate the filter query params. Returns a dict."""
    period = (request.query_params.get("period") or "month").lower()
    if period not in PERIOD_TRUNCS:
        period = "month"

    date_from = _parse_date(request.query_params.get("date_from"))
    date_to = _parse_date(request.query_params.get("date_to"))

    # Sensible default: last 12 months ending today
    if not date_to:
        date_to = date.today()
    if not date_from:
        date_from = date_to - timedelta(days=365)

    currency = (request.query_params.get("currency") or "all").strip() or "all"
    customer = (request.query_params.get("customer") or "").strip() or None
    partner = (request.query_params.get("partner") or "").strip() or None
    status_param = (request.query_params.get("status") or "completed").lower()

    return {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "currency": currency,
        "customer": customer,
        "partner": partner,
        "status": status_param,
    }


def _payments_qs(filters):
    """Build the IncomingPayment queryset with filters applied."""
    qs = IncomingPayment.objects.all()

    # Date bounds based on created_at (most meaningful — when the payment
    # was recorded). If you prefer completed_at, swap here.
    qs = qs.filter(
        created_at__date__gte=filters["date_from"],
        created_at__date__lte=filters["date_to"],
    )

    if filters["currency"] and filters["currency"] != "all":
        qs = qs.filter(currency_id=filters["currency"])
    if filters["customer"]:
        qs = qs.filter(customer_id=filters["customer"])
    if filters["status"] != "all":
        # Only profit from COMPLETED payments counts as "closed / earned".
        if filters["status"] == "completed":
            qs = qs.filter(status=TransactionStatus.COMPLETED)
        else:
            qs = qs.filter(status=filters["status"])

    return qs


def _partner_ledger_qs(filters):
    """Build the PartnerLedgerEntry queryset with filters applied."""
    qs = PartnerLedgerEntry.objects.select_related("partner", "payment")
    qs = qs.filter(
        payment__created_at__date__gte=filters["date_from"],
        payment__created_at__date__lte=filters["date_to"],
    )
    if filters["currency"] and filters["currency"] != "all":
        qs = qs.filter(payment__currency_id=filters["currency"])
    if filters["customer"]:
        qs = qs.filter(payment__customer_id=filters["customer"])
    if filters["partner"]:
        qs = qs.filter(partner_id=filters["partner"])
    if filters["status"] != "all":
        if filters["status"] == "completed":
            qs = qs.filter(payment__status=TransactionStatus.COMPLETED)
        else:
            qs = qs.filter(payment__status=filters["status"])
    return qs


def _expenses_qs(filters):
    """Build the Expense queryset for the same period (no currency/status filters)."""
    qs = Expense.objects.all()
    qs = qs.filter(
        spent_on__gte=filters["date_from"],
        spent_on__lte=filters["date_to"],
    )
    if filters["currency"] and filters["currency"] != "all":
        qs = qs.filter(currency_id=filters["currency"])
    return qs


def _compute_report(filters):
    """Main aggregation. Returns the full report payload (dict)."""
    trunc = PERIOD_TRUNCS[filters["period"]]

    # ── Per-bucket aggregates: received, fees, profit (from completed) ─
    payments = _payments_qs(filters)
    bucket_rows = (
        payments
        .annotate(period_start=trunc("created_at"))
        .values("period_start")
        .annotate(
            tx_count=Count("id"),
            total_received_foreign=Sum("amount"),
            total_received_pkr=Sum("gross_pkr"),
            total_fees_pkr=Sum(
                F("gross_pkr") - F("net_pkr"),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            total_net_pkr=Sum("net_pkr"),
        )
        .order_by("period_start")
    )

    buckets = []
    for row in bucket_rows:
        period_start = row["period_start"]
        if hasattr(period_start, "date"):
            period_start = period_start.date()
        buckets.append({
            "period_start": period_start.isoformat() if period_start else None,
            "period_label": _format_period_label(filters["period"], period_start)
                            if period_start else "—",
            "tx_count": row["tx_count"] or 0,
            "total_received_foreign": str(row["total_received_foreign"] or 0),
            "total_received_pkr": str(row["total_received_pkr"] or 0),
            "total_fees_pkr": str(row["total_fees_pkr"] or 0),
            "total_net_pkr": str(row["total_net_pkr"] or 0),
        })

    # ── Grand totals ──────────────────────────────────────────────────
    totals_row = payments.aggregate(
        tx_count=Count("id"),
        total_received_pkr=Sum("gross_pkr"),
        total_fees_pkr=Sum(
            F("gross_pkr") - F("net_pkr"),
            output_field=DecimalField(max_digits=18, decimal_places=2),
        ),
        total_net_pkr=Sum("net_pkr"),
    )

    # ── Customer rollup (top 200 by profit) ───────────────────────────
    customer_rows = (
        payments
        .values("customer_id", "customer__email", "customer__full_name")
        .annotate(
            tx_count=Count("id"),
            total_received_pkr=Sum("gross_pkr"),
            total_fees_pkr=Sum(
                F("gross_pkr") - F("net_pkr"),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            total_net_pkr=Sum("net_pkr"),
        )
        .order_by("-total_fees_pkr")[:200]
    )
    customer_rollup = [
        {
            "customer_id": str(r["customer_id"]),
            "email": r["customer__email"],
            "full_name": r["customer__full_name"] or "",
            "tx_count": r["tx_count"] or 0,
            "total_received_pkr": str(r["total_received_pkr"] or 0),
            "total_fees_pkr": str(r["total_fees_pkr"] or 0),
            "total_net_pkr": str(r["total_net_pkr"] or 0),
        }
        for r in customer_rows
    ]

    # ── Partner rollup ────────────────────────────────────────────────
    partner_ledger = _partner_ledger_qs(filters)
    partner_rows = (
        partner_ledger
        .values("partner_id", "partner__name")
        .annotate(
            tx_count=Count("id"),
            total_pkr=Sum("amount_pkr"),
        )
        .order_by("-total_pkr")
    )
    partner_rollup = [
        {
            "partner_id": str(r["partner_id"]),
            "partner_name": r["partner__name"],
            "tx_count": r["tx_count"] or 0,
            "total_pkr": str(r["total_pkr"] or 0),
        }
        for r in partner_rows
    ]

    # ── Expenses totals ───────────────────────────────────────────────
    expenses = _expenses_qs(filters)
    expense_by_currency = list(
        expenses.values("currency_id")
                .annotate(total=Sum("amount"), count=Count("id"))
                .order_by("currency_id")
    )
    # Convert all expenses (PKR + foreign) into a single PKR total
    # using the current ExchangeRate.rate_to_pkr per currency. This
    # is what gets subtracted from fees in the net-profit line so
    # USD/EUR/etc expenses don't silently disappear from the math
    # the way they did when only PKR expenses were considered.
    # PKR rate is implicit (1:1) — for any other code we look up
    # the configured rate; if a row is missing for a currency we
    # log it and skip rather than crash, since adding a missing
    # rate row is an admin-fixable issue.
    # Build a currency→PKR rate map. Static `ExchangeRate` rows take
    # priority (admin-managed). Missing currencies fall back to the
    # most recent transaction-level `exchange_rate` we've ever
    # recorded for that currency — better than silently dropping
    # USD expenses just because no admin remembered to add an
    # ExchangeRate row.
    from myapp.Models.Rate_models import ExchangeRate
    rate_map = {
        r.currency_id: Decimal(r.rate_to_pkr)
        for r in ExchangeRate.objects.all()
    }
    rate_map["PKR"] = Decimal("1")

    # Discover currencies that appear in expenses but DON'T have an
    # ExchangeRate row. For each, look up the most recent payment in
    # that currency and use its exchange rate as a sane fallback.
    # This keeps foreign-currency expenses visible in the profit
    # breakdown even when the rate table is incomplete.
    expense_currency_ids = {r["currency_id"] for r in expense_by_currency}
    missing = expense_currency_ids - set(rate_map.keys())
    if missing:
        for ccy in missing:
            recent = (
                IncomingPayment.objects
                .filter(currency_id=ccy)
                .exclude(exchange_rate__isnull=True)
                .order_by("-created_at")
                .values_list("exchange_rate", flat=True)
                .first()
            )
            if recent:
                rate_map[ccy] = Decimal(recent)

    total_expense_pkr_all = Decimal("0")
    expense_by_currency_with_pkr = []
    for r in expense_by_currency:
        ccy = r["currency_id"]
        amt = Decimal(r["total"] or 0)
        rate = rate_map.get(ccy)
        pkr_equiv = (amt * rate) if rate is not None else None
        if pkr_equiv is not None:
            total_expense_pkr_all += pkr_equiv
        expense_by_currency_with_pkr.append({
            "currency": ccy,
            "total": str(amt),
            "count": r["count"],
            # PKR equivalent at the current configured rate; null if
            # no rate is set for that currency.
            "total_pkr_equiv": str(pkr_equiv) if pkr_equiv is not None else None,
        })

    expense_totals = {
        "count": expenses.count(),
        "by_currency": expense_by_currency_with_pkr,
        # PKR-only subtotal (kept for back-compat with the
        # individual-currency breakdown card).
        "total_pkr_only": str(
            expenses.filter(currency_id="PKR").aggregate(
                total=Sum("amount"))["total"] or 0
        ),
        # New: total of ALL expenses (PKR + foreign converted at
        # current rate). This is the authoritative number used in
        # the net-profit formula. The composer's "Less: Expenses"
        # line uses this so foreign-currency expenses no longer
        # silently disappear.
        "total_pkr_equivalent": str(total_expense_pkr_all),
    }

    # ── Partner share breakdown ──────────────────────────────────────
    # For transparency, also report each partner's currently-configured share %.
    # NOTE: historical ledger entries carry `share_snapshot`; this is just a
    # reference point for "what's the current split today".
    current_shares = {
        str(ps.partner_id): str(ps.percentage)
        for ps in PartnerShare.objects.select_related("partner").all()
    }
    for row in partner_rollup:
        row["current_share_pct"] = current_shares.get(
            row["partner_id"], "0",
        )

    return {
        "filters": {
            "period": filters["period"],
            "date_from": filters["date_from"].isoformat(),
            "date_to": filters["date_to"].isoformat(),
            "currency": filters["currency"],
            "customer": filters["customer"],
            "partner": filters["partner"],
            "status": filters["status"],
        },
        "buckets": buckets,
        "totals": {
            "tx_count": totals_row["tx_count"] or 0,
            "total_received_pkr": str(totals_row["total_received_pkr"] or 0),
            "total_fees_pkr": str(totals_row["total_fees_pkr"] or 0),
            "total_net_pkr": str(totals_row["total_net_pkr"] or 0),
            # Net profit = fees collected - partner payouts - all expenses
            # (PKR + foreign converted at current rate).
            "net_profit_pkr": _compute_net_profit(
                totals_row["total_fees_pkr"],
                partner_rollup,
                expense_totals["total_pkr_equivalent"],
            ),
        },
        "customer_rollup": customer_rollup,
        "partner_rollup": partner_rollup,
        "expense_totals": expense_totals,
    }


def __uuid(s):
    """Kept for backwards compat — unused."""
    import uuid as _uuid
    try:
        return _uuid.UUID(str(s))
    except Exception:
        return s


def _compute_net_profit(total_fees_pkr, partner_rollup, total_expense_pkr):
    """
    Company's net profit from the fees collected.

    The fee on each transaction is split between the company and the
    partners pro-rata of the partner share pool. Partner payouts are
    therefore ALREADY a slice of `total_fees_pkr` — subtracting them a
    second time would double-count.

    Correct formula:
        company_share_of_fees  = total_fees_pkr − partner_payouts
        net_profit             = company_share_of_fees − expenses

    Which is algebraically:
        net = fees − partner_payouts − expenses

    That's what the previous version computed. But under the old broken
    distribution math (`fee × share/100`), partner payouts were tiny
    (e.g. 0.5% instead of 5/12 ≈ 42%), so the subtraction left most of
    the fee as "profit". After the pool-based fix, partner payouts are
    the FULL pro-rata slice — and in cases where shares sum close to
    100% (or whenever pool math consumes the whole fee), the formula
    correctly produces 0 remaining. What changed is not the formula,
    it's the meaning of partner_payouts.

    However: when the old broken ledger entries are still in the DB,
    `partner_rollup` shows the OLD tiny amounts, so `fees − partners`
    looks too generous. When the recompute command has been run with
    the pool fix, partner_rollup sums to exactly `fees`, leaving 0 for
    the company. That IS correct given the partners' shares — a pool
    covering 100% of the fee leaves nothing for the company.

    For the typical Bitnex config where partner shares sum to e.g. 12%,
    partner_rollup sums to fees × 12/12 = fees (!). That is the bug
    you're hitting. The fix: partner payouts under the pool model
    always equal the full fee, regardless of the pool size. The
    company's slice of the fee is separate from this — it's `fees ×
    (1 − pool/100)` where pool = sum of active shares.

    So the net-profit formula has to be rewritten in terms of the POOL
    not the partner payouts:

        company_retained = fees × (100 − pool) / 100
        net_profit       = company_retained − expenses

    We no longer need partner_rollup at all for this calculation; we
    use the live partner_share_pool percentage.
    """
    from myapp.Models.Partner_models import Partner

    fees = Decimal(str(total_fees_pkr or 0))
    expenses = Decimal(str(total_expense_pkr or 0))

    # Sum of active partner shares — the "pool" that gets the fee slices.
    # The rest is retained by the company.
    pool = Decimal("0")
    for p in Partner.objects.filter(is_active=True).select_related("share"):
        share = getattr(p, "share", None)
        if share and share.percentage and share.percentage > 0:
            pool += Decimal(str(share.percentage))
    # Clamp — pool shouldn't exceed 100, but sanity check anyway.
    if pool > Decimal("100"):
        pool = Decimal("100")

    company_retained = fees * ((Decimal("100") - pool) / Decimal("100"))
    net = company_retained - expenses
    return str(net.quantize(Decimal("0.01")))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def closing_report(request):
    filters = _build_filters(request)
    report = _compute_report(filters)
    return Response(report)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def closing_report_csv(request):
    """Downloadable CSV with the per-bucket figures + grand totals."""
    filters = _build_filters(request)
    report = _compute_report(filters)

    buf = StringIO()
    writer = csv.writer(buf)

    # Header block
    writer.writerow(["PayBitnex Closing Report"])
    writer.writerow([
        "Period:", filters["period"],
        "From:", filters["date_from"].isoformat(),
        "To:", filters["date_to"].isoformat(),
        "Currency:", filters["currency"],
        "Status:", filters["status"],
    ])
    writer.writerow([])

    # Buckets
    writer.writerow(["Period", "Transactions", "Received (PKR)",
                     "Fees (PKR)", "Net Paid to Customers (PKR)"])
    for b in report["buckets"]:
        writer.writerow([
            b["period_label"], b["tx_count"], b["total_received_pkr"],
            b["total_fees_pkr"], b["total_net_pkr"],
        ])
    writer.writerow([])
    writer.writerow(["TOTAL", report["totals"]["tx_count"],
                     report["totals"]["total_received_pkr"],
                     report["totals"]["total_fees_pkr"],
                     report["totals"]["total_net_pkr"]])
    writer.writerow([])
    writer.writerow(["Net Profit (fees - partners - PKR expenses):",
                     report["totals"]["net_profit_pkr"]])
    writer.writerow([])

    # Customer rollup
    writer.writerow(["-- Customer Rollup (top 200 by fees) --"])
    writer.writerow(["Customer", "Email", "Transactions",
                     "Received (PKR)", "Fees (PKR)", "Net Paid (PKR)"])
    for c in report["customer_rollup"]:
        writer.writerow([
            c["full_name"], c["email"], c["tx_count"],
            c["total_received_pkr"], c["total_fees_pkr"], c["total_net_pkr"],
        ])
    writer.writerow([])

    # Partner rollup
    writer.writerow(["-- Partner Rollup --"])
    writer.writerow(["Partner", "Current Share %", "Transactions", "PKR Earned"])
    for p in report["partner_rollup"]:
        writer.writerow([
            p["partner_name"], p["current_share_pct"],
            p["tx_count"], p["total_pkr"],
        ])
    writer.writerow([])

    # Expenses
    writer.writerow(["-- Expenses --"])
    writer.writerow(["Currency", "Count", "Total"])
    for e in report["expense_totals"]["by_currency"]:
        writer.writerow([e["currency"], e["count"], e["total"]])

    filename = (
        f"paybitnex-closing-{filters['period']}-"
        f"{filters['date_from']}-to-{filters['date_to']}.csv"
    )
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


# ── PDF endpoint ─────────────────────────────────────────────────────
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def closing_report_pdf(request):
    """
    Downloadable branded PDF. Query params:
      type = general-simple | general-full | customers | partners
             | expenses | comprehensive   (default: general-full)
      …plus all the normal filters.
    """
    from myapp.Utils.pdf_report import PDFReportBuilder
    from myapp.Views.Closing_report_composers import REPORT_TYPES

    filters = _build_filters(request)
    report = _compute_report(filters)

    report_type = (request.query_params.get("type")
                   or "general-full").strip().lower()
    if report_type not in REPORT_TYPES:
        report_type = "general-full"

    title, composer = REPORT_TYPES[report_type]
    sections = composer(report)

    # Profit-type context — gross (default) or net. This doesn't change the
    # numeric contents (all composers include both gross fees AND net
    # profit), but it tweaks the subtitle + the leading KPI emphasis so
    # the reader sees the mode the admin intended.
    profit_type = (request.query_params.get("profit_type") or "gross").lower()
    if profit_type not in ("gross", "net"):
        profit_type = "gross"

    # Build subtitle describing the date range + filters in natural language
    subtitle_parts = [
        f"{filters['date_from'].strftime('%b %d, %Y')} — "
        f"{filters['date_to'].strftime('%b %d, %Y')}"
    ]
    if filters["currency"] != "all":
        subtitle_parts.append(f"{filters['currency']} only")
    if filters["status"] and filters["status"] != "completed":
        subtitle_parts.append(f"status: {filters['status']}")
    subtitle_parts.append(
        "Net profit mode" if profit_type == "net" else "Gross profit mode"
    )
    subtitle = " · ".join(subtitle_parts)

    # Build metadata grid shown at the top of page 1
    metadata = {
        "Date Range": f"{filters['date_from']} to {filters['date_to']}",
        "Bucket":     filters["period"].capitalize(),
        "Currency":   "All currencies" if filters["currency"] == "all"
                      else filters["currency"],
        "Status":     filters["status"].replace("_", " ").capitalize(),
        "Profit Mode": "Net (fees − partners − expenses)"
                       if profit_type == "net" else "Gross (fees only)",
        "Generated By": request.user.full_name or request.user.email,
        "Generated On": datetime.now().strftime("%b %d, %Y at %H:%M"),
    }

    builder = PDFReportBuilder(
        title=title, subtitle=subtitle, metadata=metadata,
        # Header band reflects the report type so each PDF
        # self-identifies — comprehensive ("CLOSING REPORT"),
        # expenses ("EXPENSES REPORT"), partners ("PARTNERS
        # REPORT"). Falls back to the generic label otherwise.
        header_label={
            "comprehensive": "CLOSING REPORT",
            "expenses":      "EXPENSES REPORT",
            "partners":      "PARTNERS REPORT",
        }.get(report_type, "CLOSING REPORT"),
    )
    pdf_bytes = builder.build(sections)

    filename = (
        f"paybitnex-{report_type}-"
        f"{filters['date_from']}-to-{filters['date_to']}.pdf"
    )
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
