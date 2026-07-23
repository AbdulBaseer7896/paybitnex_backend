"""
Projected settlement report — "how much do we still owe customers?"

THE PROBLEM THIS SOLVES
-----------------------
The dashboard computes PKR only once an accountant applies a real dollar
rate. Until then `gross_pkr` / `net_pkr` are NULL, so a week with ten
live transactions and $8,564 received still shows:

    Total received (PKR)         Rs 0.00
    PKR transferred to customers Rs 0.00
    Rate-spread profit (PKR)     Rs 0.00

Nobody can see how much money is outstanding, because the only figures
that exist are the ones already processed.

This report answers that question by PROJECTING every unprocessed
transaction at the default dollar rate: "if we settled everything at
today's default rate, what would we owe, and what would we earn?"

PROJECTED vs ACTUAL — the core rule
-----------------------------------
Each transaction is classified:

  ACTUAL    — accountant applied a real rate AND fee. Figures come
              straight from the stored columns. Never recomputed.
  PROJECTED — no real rate yet. Figures computed on the fly from the
              default dollar rate and the customer's effective fee.

Projections are NEVER written to the database. This endpoint is
read-only: it computes in memory and returns. The accountant applying a
real rate remains the single source of truth, and nothing here can
corrupt a partner ledger or a customer payout.

Every total is returned three ways — actual, projected, and combined —
so the UI can always show what is real money versus an estimate.

RATE + FEE RESOLUTION (mirrors the live pipeline exactly)
---------------------------------------------------------
  rate = SystemSetting["default_dollar_rate"]  →  ExchangeRate table
  fee  = CustomerFeeConfig[customer]           →  SystemSetting
         ["default_fee_percentage"]            →  5.00

The arithmetic below is a line-for-line mirror of
`IncomingPayment.calculate_amounts()`, including the same
`quantize(0.01)` rounding at each step, so a projected figure equals what
the row will show once the accountant applies that same rate.

REJECTED transactions are excluded everywhere — that money never existed.
"""
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import DateField
from django.db.models.functions import Coalesce, TruncDate
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
from myapp.Utils.bitnex_week import get_week_config, WEEKDAY_NAMES
from myapp.Utils.permissions import IsAdminOrAccountant


TWOPLACES = Decimal("0.01")

# Everything except rejected — that money was never received.
REPORTABLE_STATUSES = [
    s for s, _ in TransactionStatus.choices
    if s != TransactionStatus.REJECTED
]

DEFAULT_WEEKS = 8
MAX_WEEKS = 104


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _dec(value):
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def week_start_for(day, start_day=0):
    """Week-start for the week containing `day`. Monday=0 … Sunday=6."""
    delta = (day.weekday() - start_day) % 7
    return day - timedelta(days=delta)


def _resolve_default_rate(currency_code="USD"):
    """The projection rate. Same order as Utils/default_rate.py."""
    from myapp.Utils.default_rate import get_default_rate
    return get_default_rate(currency_code)


def _build_fee_map():
    """customer_id -> effective fee %, so we don't query per transaction."""
    from myapp.Models.Core_models import SystemSetting
    from myapp.Models.Fee_models import CustomerFeeConfig

    default_fee = Decimal("5.00")
    raw = SystemSetting.get("default_fee_percentage", None)
    if raw:
        try:
            default_fee = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            pass

    overrides = {
        row["customer_id"]: _dec(row["fee_percentage"])
        for row in CustomerFeeConfig.objects.values(
            "customer_id", "fee_percentage",
        )
    }
    return overrides, default_fee


def _blank_bucket():
    """Zeroed accumulator. `projected_*` never mixes into `actual_*`."""
    return {
        "tx_count": 0,
        "actual_count": 0,
        "projected_count": 0,
        "amount_usd": Decimal("0"),
        "actual_amount_usd": Decimal("0"),
        "projected_amount_usd": Decimal("0"),
        # Gross PKR = amount x rate
        "actual_gross_pkr": Decimal("0"),
        "projected_gross_pkr": Decimal("0"),
        # Net PKR = what the customer receives (owed / transferred)
        "actual_net_pkr": Decimal("0"),
        "projected_net_pkr": Decimal("0"),
        # Fee revenue in PKR = gross - net
        "actual_fee_pkr": Decimal("0"),
        "projected_fee_pkr": Decimal("0"),
        # Rate-spread profit - ACTUAL ONLY. See _compute_row().
        "actual_spread_pkr": Decimal("0"),
        # Already settled vs still outstanding (net PKR).
        "settled_net_pkr": Decimal("0"),
        "outstanding_net_pkr": Decimal("0"),
    }


def _accumulate(bucket, row):
    bucket["tx_count"] += 1
    bucket["amount_usd"] += row["amount"]
    if row["is_projected"]:
        bucket["projected_count"] += 1
        bucket["projected_amount_usd"] += row["amount"]
        bucket["projected_gross_pkr"] += row["gross_pkr"]
        bucket["projected_net_pkr"] += row["net_pkr"]
        bucket["projected_fee_pkr"] += row["fee_pkr"]
    else:
        bucket["actual_count"] += 1
        bucket["actual_amount_usd"] += row["amount"]
        bucket["actual_gross_pkr"] += row["gross_pkr"]
        bucket["actual_net_pkr"] += row["net_pkr"]
        bucket["actual_fee_pkr"] += row["fee_pkr"]
        bucket["actual_spread_pkr"] += row["spread_pkr"]

    if row["is_settled"]:
        bucket["settled_net_pkr"] += row["net_pkr"]
    else:
        bucket["outstanding_net_pkr"] += row["net_pkr"]


def _serialise_bucket(b):
    """Combined = actual + projected, so the UI can show one headline."""
    return {
        "tx_count": b["tx_count"],
        "actual_count": b["actual_count"],
        "projected_count": b["projected_count"],
        "amount_usd": str(b["amount_usd"]),
        "actual_amount_usd": str(b["actual_amount_usd"]),
        "projected_amount_usd": str(b["projected_amount_usd"]),
        "gross_pkr": str(b["actual_gross_pkr"] + b["projected_gross_pkr"]),
        "actual_gross_pkr": str(b["actual_gross_pkr"]),
        "projected_gross_pkr": str(b["projected_gross_pkr"]),
        "net_pkr": str(b["actual_net_pkr"] + b["projected_net_pkr"]),
        "actual_net_pkr": str(b["actual_net_pkr"]),
        "projected_net_pkr": str(b["projected_net_pkr"]),
        "fee_pkr": str(b["actual_fee_pkr"] + b["projected_fee_pkr"]),
        "actual_fee_pkr": str(b["actual_fee_pkr"]),
        "projected_fee_pkr": str(b["projected_fee_pkr"]),
        # Spread is actual-only by design - see _compute_row().
        "spread_pkr": str(b["actual_spread_pkr"]),
        "settled_net_pkr": str(b["settled_net_pkr"]),
        "outstanding_net_pkr": str(b["outstanding_net_pkr"]),
        "has_projected": b["projected_count"] > 0,
    }


def _compute_row(payment, default_rate, fee_overrides, default_fee):
    """Resolve one payment into actual or projected figures."""
    amount = _dec(payment.amount)

    has_real_rate = (
        payment.exchange_rate is not None
        and not getattr(payment, "is_rate_provisional", False)
        and payment.fee_percentage is not None
    )

    if has_real_rate:
        rate = _dec(payment.exchange_rate)
        fee_pct = _dec(payment.fee_percentage)
        # The fee was locked in on the transaction itself at apply time.
        fee_source = "transaction"
        # Trust the stored columns; only recompute if a legacy row never
        # had them populated.
        gross = (
            _dec(payment.gross_pkr) if payment.gross_pkr is not None
            else (amount * rate).quantize(TWOPLACES)
        )
        if payment.net_pkr is not None:
            net = _dec(payment.net_pkr)
        else:
            fee_fx = (amount * (fee_pct / Decimal("100"))).quantize(TWOPLACES)
            net = ((amount - fee_fx) * rate).quantize(TWOPLACES)

        # Rate-spread profit needs the REAL market rate, which only exists
        # once an accountant records it. It is therefore never projected -
        # inventing a spread would fabricate profit from nothing.
        spread = Decimal("0")
        if payment.real_exchange_rate is not None:
            diff = _dec(payment.real_exchange_rate) - rate
            if diff > 0:
                spread = (amount * diff).quantize(TWOPLACES)

        is_projected = False
    else:
        rate = default_rate
        # Per-customer override (Settings -> Fee config) wins; otherwise
        # the system-wide default. Reporting which one applied makes a
        # uniform fee across customers obviously "no override set"
        # rather than looking like the override was ignored.
        if payment.customer_id in fee_overrides:
            fee_pct = fee_overrides[payment.customer_id]
            fee_source = "customer_override"
        else:
            fee_pct = default_fee
            fee_source = "system_default"
        if rate is None:
            gross = net = Decimal("0")
        else:
            # Mirrors calculate_amounts() step for step, same rounding.
            fee_fx = (amount * (fee_pct / Decimal("100"))).quantize(TWOPLACES)
            net_fx = (amount - fee_fx).quantize(TWOPLACES)
            gross = (amount * rate).quantize(TWOPLACES)
            net = (net_fx * rate).quantize(TWOPLACES)
        spread = Decimal("0")
        is_projected = True

    return {
        "amount": amount,
        "rate": rate,
        "fee_pct": fee_pct,
        "fee_source": fee_source,
        "gross_pkr": gross,
        "net_pkr": net,
        "fee_pkr": (gross - net),
        "spread_pkr": spread,
        "is_projected": is_projected,
        # "Settled" = the customer has actually been paid.
        "is_settled": payment.status == TransactionStatus.COMPLETED,
    }


def _filtered_qs(request):
    """Shared queryset + filter parsing for both endpoints."""
    qs = (
        IncomingPayment.objects
        .filter(status__in=REPORTABLE_STATUSES)
        .select_related("customer", "currency")
    )

    status_param = (request.query_params.get("status") or "").strip().lower()
    if status_param and status_param not in ("all", "any"):
        if status_param == TransactionStatus.REJECTED:
            return None, None, Response(
                {"detail": "Rejected transactions are excluded from this "
                           "report."},
                status=400,
            )
        if status_param not in REPORTABLE_STATUSES:
            return None, None, Response(
                {"detail": f"Unknown status '{status_param}'."}, status=400,
            )
        qs = qs.filter(status=status_param)

    currency = (request.query_params.get("currency") or "all").strip()
    if currency and currency.lower() != "all":
        qs = qs.filter(currency_id=currency)

    customer = (request.query_params.get("customer") or "").strip()
    if customer:
        qs = qs.filter(customer_id=customer)

    # Business date, falling back to entry date for legacy rows.
    qs = qs.annotate(
        tx_date=Coalesce(
            "occurred_on", TruncDate("created_at"), output_field=DateField(),
        ),
    )
    return qs, {"status": status_param or "all_except_rejected",
                "currency": currency, "customer": customer or None}, None


# ---------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def weekly_monday_report(request):
    """GET /reports/weekly-monday/

    Projected settlement across a date range, grouped BY CUSTOMER and by
    week. Answers "how much have we not yet transferred?"

    Params: date_from, date_to, weeks, status, currency, customer.
    """
    cfg = get_week_config()
    start_day = cfg["start_day"]
    today = date.today()

    qs, filters, err = _filtered_qs(request)
    if err is not None:
        return err

    date_from = _parse_date(request.query_params.get("date_from"))
    date_to = _parse_date(request.query_params.get("date_to"))
    if not date_to:
        date_to = today
    if not date_from:
        try:
            weeks_back = int(request.query_params.get("weeks") or DEFAULT_WEEKS)
        except (TypeError, ValueError):
            weeks_back = DEFAULT_WEEKS
        weeks_back = max(1, min(weeks_back, MAX_WEEKS))
        date_from = week_start_for(today, start_day) - timedelta(
            days=7 * (weeks_back - 1)
        )
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    range_start = week_start_for(date_from, start_day)
    range_end = week_start_for(date_to, start_day) + timedelta(days=6)
    if ((range_end - range_start).days // 7) + 1 > MAX_WEEKS:
        range_start = week_start_for(range_end, start_day) - timedelta(
            days=7 * (MAX_WEEKS - 1)
        )

    qs = qs.filter(tx_date__gte=range_start, tx_date__lte=range_end)

    default_rate = _resolve_default_rate()
    fee_overrides, default_fee = _build_fee_map()

    grand = _blank_bucket()
    by_customer = OrderedDict()
    by_week = OrderedDict()
    by_status = OrderedDict()

    for payment in qs.iterator(chunk_size=500):
        row = _compute_row(payment, default_rate, fee_overrides, default_fee)

        _accumulate(grand, row)

        cid = str(payment.customer_id)
        if cid not in by_customer:
            by_customer[cid] = {
                "customer_id": cid,
                "email": getattr(payment.customer, "email", ""),
                "full_name": getattr(payment.customer, "full_name", "") or "",
                "bucket": _blank_bucket(),
                "statuses": {},
            }
        _accumulate(by_customer[cid]["bucket"], row)
        by_customer[cid]["statuses"][payment.status] = (
            by_customer[cid]["statuses"].get(payment.status, 0) + 1
        )

        tx_date = payment.tx_date
        if hasattr(tx_date, "date"):
            tx_date = tx_date.date()
        if tx_date:
            wk = week_start_for(tx_date, start_day)
            if wk not in by_week:
                by_week[wk] = _blank_bucket()
            _accumulate(by_week[wk], row)

        if payment.status not in by_status:
            by_status[payment.status] = _blank_bucket()
        _accumulate(by_status[payment.status], row)

    status_labels = dict(TransactionStatus.choices)

    customers = sorted(
        by_customer.values(),
        key=lambda c: c["bucket"]["actual_net_pkr"]
        + c["bucket"]["projected_net_pkr"],
        reverse=True,
    )

    return Response({
        "week_start_day": start_day,
        "week_start_day_name": WEEKDAY_NAMES[start_day],
        "week_name": cfg["name"],
        "range": {"from": range_start.isoformat(), "to": range_end.isoformat()},
        "projection": {
            "default_rate": str(default_rate) if default_rate else None,
            "default_fee_percentage": str(default_fee),
            "rate_available": default_rate is not None,
            "note": (
                "Transactions without an accountant-applied rate are "
                "projected at the default dollar rate. Projections are "
                "estimates and are never saved to the database."
            ),
        },
        "totals": _serialise_bucket(grand),
        "by_customer": [
            {
                "customer_id": c["customer_id"],
                "email": c["email"],
                "full_name": c["full_name"],
                "statuses": [
                    {
                        "status": st,
                        "label": status_labels.get(st, st),
                        "count": n,
                    }
                    for st, n in sorted(
                        c["statuses"].items(), key=lambda kv: -kv[1],
                    )
                ],
                **_serialise_bucket(c["bucket"]),
            }
            for c in customers
        ],
        "by_week": [
            {
                "week_start": wk.isoformat(),
                "week_end": (wk + timedelta(days=6)).isoformat(),
                "next_week_start": (wk + timedelta(days=7)).isoformat(),
                "label": (
                    f"{wk.strftime('%a %d %b')} -> "
                    f"{(wk + timedelta(days=7)).strftime('%a %d %b %Y')}"
                ),
                "is_current": wk == week_start_for(today, start_day),
                **_serialise_bucket(b),
            }
            for wk, b in sorted(by_week.items(), reverse=True)
        ],
        "by_status": [
            {
                "status": st,
                "label": status_labels.get(st, st),
                **_serialise_bucket(b),
            }
            for st, b in sorted(
                by_status.items(), key=lambda kv: -kv[1]["tx_count"],
            )
        ],
        "filters": filters,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def weekly_monday_detail(request):
    """GET /reports/weekly-monday/detail/

    Line items for the spreadsheet view. Accepts either
    `week_start=YYYY-MM-DD` or the same date_from/date_to as the summary,
    plus an optional `customer` filter.
    """
    cfg = get_week_config()
    start_day = cfg["start_day"]

    qs, filters, err = _filtered_qs(request)
    if err is not None:
        return err

    anchor = _parse_date(request.query_params.get("week_start"))
    if anchor:
        wk_start = week_start_for(anchor, start_day)
        wk_end = wk_start + timedelta(days=6)
    else:
        wk_start = _parse_date(request.query_params.get("date_from"))
        wk_end = _parse_date(request.query_params.get("date_to"))
        if not wk_end:
            wk_end = date.today()
        if not wk_start:
            wk_start = week_start_for(wk_end, start_day) - timedelta(
                days=7 * (DEFAULT_WEEKS - 1)
            )

    qs = qs.filter(tx_date__gte=wk_start, tx_date__lte=wk_end).order_by(
        "customer__email", "tx_date", "reference",
    )

    default_rate = _resolve_default_rate()
    fee_overrides, default_fee = _build_fee_map()
    status_labels = dict(TransactionStatus.choices)

    items = []
    totals = _blank_bucket()
    for p in qs[:5000]:
        row = _compute_row(p, default_rate, fee_overrides, default_fee)
        _accumulate(totals, row)
        tx_date = p.tx_date
        if hasattr(tx_date, "date"):
            tx_date = tx_date.date()
        items.append({
            "id": str(p.id),
            "reference": p.reference,
            "tx_date": tx_date.isoformat() if tx_date else None,
            "customer_id": str(p.customer_id),
            "customer_email": getattr(p.customer, "email", ""),
            "customer_name": getattr(p.customer, "full_name", "") or "",
            "sender_name": p.sender_name,
            "sender_company": p.sender_company,
            "currency": p.currency_id,
            "amount": str(row["amount"]),
            "rate": str(row["rate"]) if row["rate"] else None,
            "fee_percentage": str(row["fee_pct"]),
            "fee_source": row["fee_source"],
            "gross_pkr": str(row["gross_pkr"]),
            "net_pkr": str(row["net_pkr"]),
            "fee_pkr": str(row["fee_pkr"]),
            "spread_pkr": str(row["spread_pkr"]),
            "is_projected": row["is_projected"],
            "is_settled": row["is_settled"],
            "status": p.status,
            "status_label": status_labels.get(p.status, p.status),
        })

    return Response({
        "range": {"from": wk_start.isoformat(), "to": wk_end.isoformat()},
        "projection": {
            "default_rate": str(default_rate) if default_rate else None,
            "default_fee_percentage": str(default_fee),
            "rate_available": default_rate is not None,
        },
        "count": len(items),
        "totals": _serialise_bucket(totals),
        "items": items,
        "filters": filters,
    })
