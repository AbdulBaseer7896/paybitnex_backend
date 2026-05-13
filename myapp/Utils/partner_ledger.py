"""
Partner ledger service — updated for dual dollar rates & fee allocation.

Distribution model:
  Each partner's % is applied to the TRANSACTION AMOUNT (not the fee).
  The company's base profit = total fee collected - sum of all partner payouts.

  Additionally, the company earns a RATE SPREAD profit:
    rate_spread_profit = (real_exchange_rate - tangent_exchange_rate) * amount
  This spread applies to the entire transaction amount (customer's net + fee portion).
  Partners only receive their share at the tangent rate — the spread on their
  portion also stays with the company.

Example: $100 transaction, 15% fee, tangent rate=271, real rate=279
  fee = $15, net_to_customer = $85
  Partner A (3%) → $3 × 271 = 813 PKR (gets tangent rate)
  Partner B (4%) → $4 × 271 = 1084 PKR (gets tangent rate)
  Company base (from fee): 8$ × 279 = 2232 PKR
  Rate spread profit: (279-271) × $100 = 800 PKR → stays with company
    - from customer's 85$: (279-271)*85 = 680
    - from partner A's $3: (279-271)*3 = 24
    - from partner B's $4: (279-271)*4 = 32
    - from company's $8: (279-271)*8 = 64 (already in company_base)
  Total company rate spread: 680+24+32+64 = 800 → but we track it separately
  per-partner too.

UNDER-FEE FIX (Update #3):
  If fee% < sum of all partner shares %, a fee_allocation JSON is stored on
  the payment specifying exactly who gets what % of the fee.
  e.g. 5% fee, partners A(3%) + B(4%) can't both get full shares.
  Admin decides: company takes all, or split with one partner, etc.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as dbtx

log = logging.getLogger(__name__)
ZERO = Decimal("0.00")
QUANT = Decimal("0.01")


def _q(x):
    return Decimal(str(x)).quantize(QUANT, rounding=ROUND_HALF_UP)


def distribute_fee_for_payment(payment) -> list:
    """
    Create PartnerLedgerEntry rows for the given IncomingPayment.

    Handles two scenarios:
    1. Normal: fee% >= sum of partner shares → standard distribution
    2. Under-fee: fee% < sum of partner shares → use payment.fee_allocation

    Each partner receives: transaction_amount × (share% / 100) [at tangent rate]
    Company also earns rate spread: (real_rate - tangent_rate) × amount

    Idempotent — if entries already exist, does nothing.
    """
    from myapp.Models.Partner_models import Partner, PartnerLedgerEntry

    if payment.fee_amount_foreign is None or payment.exchange_rate is None:
        raise ValueError("Payment must have fee_amount_foreign and exchange_rate set.")

    with dbtx.atomic():
        if PartnerLedgerEntry.objects.filter(payment=payment).exists():
            log.info("Ledger entries already exist for %s — skipping.", payment.reference)
            return list(PartnerLedgerEntry.objects.filter(payment=payment))

        # Transaction amount (e.g. $100) — partners get % of THIS
        tx_amount_foreign = _q(payment.amount)
        tangent_rate = Decimal(str(payment.exchange_rate))
        real_rate = Decimal(str(payment.real_exchange_rate or payment.exchange_rate))

        # Fee collected — company baseline before partner payouts
        fee_foreign = _q(payment.fee_amount_foreign)
        fee_pkr = _q(fee_foreign * tangent_rate)

        partners = list(
            Partner.objects.filter(is_active=True).select_related("share")
        )

        # Check if we have a fee_allocation override (under-fee scenario)
        fee_allocation = payment.fee_allocation  # dict or None

        if fee_allocation:
            # Under-fee mode: use explicit allocation JSON
            return _distribute_with_allocation(
                payment, partners, fee_allocation,
                tx_amount_foreign, fee_foreign, tangent_rate, real_rate,
            )

        # --- Normal distribution ---
        eligible = []
        total_pct = Decimal("0")
        for p in partners:
            share = getattr(p, "share", None)
            if share is None or share.percentage <= 0:
                continue
            pct = Decimal(str(share.percentage))
            eligible.append((p, pct))
            total_pct += pct

        if not eligible:
            log.info("No eligible partners for %s — fee stays with company.", payment.reference)
            return []

        # Check for under-fee condition
        fee_pct = Decimal(str(payment.fee_percentage or 0))
        if fee_pct < total_pct:
            log.warning(
                "Under-fee condition for %s: fee%%=%.2f < partner_total%%=%.2f. "
                "No fee_allocation set — skipping partner distribution.",
                payment.reference, fee_pct, total_pct,
            )
            return []

        created = []
        for p, share_pct in eligible:
            amt_foreign = _q(tx_amount_foreign * share_pct / Decimal("100"))
            amt_pkr = _q(amt_foreign * tangent_rate)

            # Rate spread profit on this partner's portion (goes to company)
            spread = max(real_rate - tangent_rate, Decimal("0"))
            rate_spread = _q(amt_foreign * spread)

            entry = PartnerLedgerEntry.objects.create(
                partner=p,
                payment=payment,
                share_snapshot=share_pct,
                fee_total_foreign=fee_foreign,
                fee_total_pkr=fee_pkr,
                amount_foreign=amt_foreign,
                amount_pkr=amt_pkr,
                currency_code=payment.currency_id,
                real_exchange_rate_snapshot=real_rate,
                rate_spread_profit_pkr=rate_spread,
            )
            created.append(entry)

        log.info(
            "Created %d partner ledger entries for %s "
            "(tx=%s %s, fee=%s, tangent=%s, real=%s)",
            len(created), payment.reference,
            tx_amount_foreign, payment.currency_id,
            fee_foreign, tangent_rate, real_rate,
        )
        return created


def _distribute_with_allocation(payment, partners, fee_allocation,
                                 tx_amount_foreign, fee_foreign,
                                 tangent_rate, real_rate):
    """
    Distribute fee using explicit fee_allocation JSON (under-fee scenario).
    
    fee_allocation format:
    {
        "company": <pct_of_fee>,
        "partners": {"<partner_uuid>": <pct_of_fee>}
    }
    pct_of_fee values must sum to 100.
    """
    from myapp.Models.Partner_models import PartnerLedgerEntry

    partner_allocs = fee_allocation.get("partners", {})
    if not partner_allocs:
        log.info("fee_allocation has no partner entries — all fee to company.")
        return []

    partner_map = {str(p.id): p for p in partners}
    fee_pkr = _q(fee_foreign * tangent_rate)
    spread = max(real_rate - tangent_rate, Decimal("0"))

    created = []
    for partner_id_str, pct_of_fee in partner_allocs.items():
        p = partner_map.get(partner_id_str)
        if not p:
            log.warning("fee_allocation: partner %s not found, skipping.", partner_id_str)
            continue

        pct = Decimal(str(pct_of_fee))
        # Partner gets pct% of the fee (not tx amount, since under-fee)
        amt_foreign = _q(fee_foreign * pct / Decimal("100"))
        amt_pkr = _q(amt_foreign * tangent_rate)
        rate_spread = _q(amt_foreign * spread)

        entry = PartnerLedgerEntry.objects.create(
            partner=p,
            payment=payment,
            share_snapshot=pct,
            fee_total_foreign=fee_foreign,
            fee_total_pkr=fee_pkr,
            amount_foreign=amt_foreign,
            amount_pkr=amt_pkr,
            currency_code=payment.currency_id,
            real_exchange_rate_snapshot=real_rate,
            rate_spread_profit_pkr=rate_spread,
        )
        created.append(entry)

    log.info(
        "Created %d partner ledger entries (allocation mode) for %s",
        len(created), payment.reference,
    )
    return created


def redistribute_fee_for_payment(payment, update_rate_only=False) -> list:
    """
    Re-run fee distribution for a completed payment.
    Used when real_exchange_rate is updated post-completion.

    If update_rate_only=True, only updates rate_spread_profit_pkr on existing
    entries without recreating them (preserves amount_pkr to customer).
    """
    from myapp.Models.Partner_models import PartnerLedgerEntry

    real_rate = Decimal(str(payment.real_exchange_rate or payment.exchange_rate))
    tangent_rate = Decimal(str(payment.exchange_rate))
    spread = max(real_rate - tangent_rate, Decimal("0"))

    with dbtx.atomic():
        if update_rate_only:
            # Only update rate spread profit on existing entries
            for entry in PartnerLedgerEntry.objects.filter(payment=payment):
                entry.real_exchange_rate_snapshot = real_rate
                entry.rate_spread_profit_pkr = _q(entry.amount_foreign * spread)
                entry.save(update_fields=[
                    "real_exchange_rate_snapshot", "rate_spread_profit_pkr"
                ])
            return list(PartnerLedgerEntry.objects.filter(payment=payment))
        else:
            # Full re-distribution: delete and recreate
            PartnerLedgerEntry.objects.filter(payment=payment).delete()
            return distribute_fee_for_payment(payment)


async def adistribute_fee_for_payment(payment) -> list:
    """Async wrapper."""
    from asgiref.sync import sync_to_async
    return await sync_to_async(distribute_fee_for_payment, thread_sensitive=True)(payment)


def partner_balance(partner, currency_code: str = "PKR") -> Decimal:
    """Sum of ledger entries for a partner in a given currency."""
    from myapp.Models.Partner_models import PartnerLedgerEntry
    from django.db.models import Sum

    field = "amount_pkr" if currency_code == "PKR" else "amount_foreign"
    filters = {"partner": partner}
    if currency_code != "PKR":
        filters["currency_code"] = currency_code
    total = (
        PartnerLedgerEntry.objects
        .filter(**filters)
        .aggregate(s=Sum(field))["s"]
    )
    return total or ZERO


def company_profit_for_payment(payment) -> dict:
    """
    Company's complete profit on a single payment:
      - Base: fee_collected − sum(partner payouts at tangent rate)
      - Rate spread: (real_rate - tangent_rate) × total_amount
      
    Returns dict with foreign and PKR amounts, plus rate_spread breakdown.
    """
    from myapp.Models.Partner_models import PartnerLedgerEntry
    from django.db.models import Sum

    tangent_rate = Decimal(str(payment.exchange_rate or 0))
    real_rate = Decimal(str(payment.real_exchange_rate or payment.exchange_rate or 0))

    fee_foreign = _q(payment.fee_amount_foreign or 0)
    fee_pkr = _q(fee_foreign * tangent_rate)

    partner_agg = PartnerLedgerEntry.objects.filter(payment=payment).aggregate(
        total_foreign=Sum("amount_foreign"),
        total_pkr=Sum("amount_pkr"),
        total_rate_spread=Sum("rate_spread_profit_pkr"),
    )
    partner_foreign = _q(partner_agg["total_foreign"] or 0)
    partner_pkr = _q(partner_agg["total_pkr"] or 0)
    partner_rate_spread = _q(partner_agg["total_rate_spread"] or 0)

    # Total rate spread on the full amount (customer net + fee)
    total_amount = _q(payment.amount or 0)
    spread = max(real_rate - tangent_rate, Decimal("0"))
    total_rate_spread_pkr = _q(total_amount * spread)

    # Company base profit (from fee margin, at tangent rate)
    company_base_foreign = fee_foreign - partner_foreign
    company_base_pkr = fee_pkr - partner_pkr

    # Company rate spread = total spread - partner spreads
    # (partners earn at tangent; company keeps the spread on all portions)
    company_own_rate_spread = total_rate_spread_pkr

    return {
        "fee_foreign": fee_foreign,
        "fee_pkr": fee_pkr,
        "partner_total_foreign": partner_foreign,
        "partner_total_pkr": partner_pkr,
        "partner_rate_spread_pkr": partner_rate_spread,
        "company_base_foreign": company_base_foreign,
        "company_base_pkr": company_base_pkr,
        "rate_spread_pkr": company_own_rate_spread,
        "company_total_pkr": company_base_pkr + company_own_rate_spread,
        # Legacy keys kept for compatibility
        "company_foreign": company_base_foreign,
        "company_pkr": company_base_pkr + company_own_rate_spread,
    }


def get_company_profit_summary(filters=None) -> dict:
    """
    Aggregate company profit across all completed transactions in a date range.
    Returns base profit (from fees) + rate spread profit separately.
    Used in dashboard, closing reports, partner page.
    """
    from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
    from myapp.Models.Partner_models import PartnerLedgerEntry
    from django.db.models import Sum, Count, F, ExpressionWrapper, DecimalField as DjDecimal

    qs = IncomingPayment.objects.filter(status=TransactionStatus.COMPLETED)
    if filters:
        if filters.get("date_from"):
            qs = qs.filter(created_at__date__gte=filters["date_from"])
        if filters.get("date_to"):
            qs = qs.filter(created_at__date__lte=filters["date_to"])

    # Sum partner payouts (at tangent rate)
    partner_totals = (
        PartnerLedgerEntry.objects
        .filter(payment__in=qs)
        .aggregate(
            total_pkr=Sum("amount_pkr"),
            total_rate_spread=Sum("rate_spread_profit_pkr"),
        )
    )
    partner_pkr = _q(partner_totals["total_pkr"] or 0)

    tx_totals = qs.aggregate(
        tx_count=Count("id"),
        total_fees_pkr=Sum(
            ExpressionWrapper(
                F("gross_pkr") - F("net_pkr"),
                output_field=DjDecimal(max_digits=18, decimal_places=2),
            )
        ),
        total_received_pkr=Sum("gross_pkr"),
    )

    total_fees_pkr = _q(tx_totals["total_fees_pkr"] or 0)

    # Rate spread profit: sum (real_rate - tangent_rate) * amount for all txns
    # For txns where real_exchange_rate is NULL (legacy), spread = 0
    rate_spread_total = Decimal("0")
    for tx in qs.filter(
        real_exchange_rate__isnull=False
    ).values("amount", "exchange_rate", "real_exchange_rate"):
        spread = max(
            Decimal(str(tx["real_exchange_rate"])) - Decimal(str(tx["exchange_rate"])),
            Decimal("0"),
        )
        rate_spread_total += _q(Decimal(str(tx["amount"])) * spread)

    company_base_pkr = total_fees_pkr - partner_pkr
    total_company_pkr = company_base_pkr + rate_spread_total

    return {
        "tx_count": tx_totals["tx_count"] or 0,
        "total_received_pkr": str(_q(tx_totals["total_received_pkr"] or 0)),
        "total_fees_pkr": str(total_fees_pkr),
        "partner_payouts_pkr": str(partner_pkr),
        "company_base_profit_pkr": str(company_base_pkr),
        "rate_spread_profit_pkr": str(rate_spread_total),
        "total_company_profit_pkr": str(total_company_pkr),
    }


def partner_expense_deduction(partner, date_from=None, date_to=None) -> Decimal:
    """
    Sum of ExpenseDistribution slices assigned to this partner (in PKR).
    """
    from myapp.Models.Expense_models import ExpenseDistribution
    from myapp.Models.Rate_models import ExchangeRate

    rates = {"PKR": Decimal("1")}
    for r in ExchangeRate.objects.all():
        rates[r.currency_id] = Decimal(str(r.rate_to_pkr or 0))

    qs = ExpenseDistribution.objects.filter(
        partner=partner,
    ).select_related("expense__currency")
    if date_from:
        qs = qs.filter(expense__spent_on__gte=date_from)
    if date_to:
        qs = qs.filter(expense__spent_on__lte=date_to)

    total = Decimal("0")
    for d in qs:
        code = d.expense.currency_id
        rate = rates.get(code) or Decimal("0")
        total += _q(Decimal(str(d.amount)) * rate)
    return total
