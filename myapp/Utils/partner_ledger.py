"""
Partner ledger service.

Distribution model (CORRECT):
  Each partner's % is applied to the TRANSACTION AMOUNT (not the fee).
  The company's profit = total fee collected - sum of all partner payouts.

Example: $100 transaction, 20% fee rate → fee = $20
         Partner A = 3%  → receives $100 × 3% = $3.00
         Partner B = 4%  → receives $100 × 4% = $4.00
         Sum of partner payouts           = $7.00
         Company profit = $20 fee − $7 payouts = $13.00

So:
  partner_amount = transaction_amount × (partner_share% / 100)
  company_profit = fee_collected − sum(all partner amounts)
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

    Each partner receives:  transaction_amount × (share% / 100)
    Company retains:        fee_collected − sum(partner payouts)

    The ledger entries store the partner's payout. The company's retained
    amount is implicit (fee_total − sum of entries). Reports show it as:
        company_profit = fee − partner_total

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
        tx_amount_pkr = _q(tx_amount_foreign * payment.exchange_rate)

        # Fee collected — company baseline before partner payouts
        fee_foreign = _q(payment.fee_amount_foreign)
        fee_pkr = _q(fee_foreign * payment.exchange_rate)

        partners = list(
            Partner.objects.filter(is_active=True).select_related("share")
        )

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

        created = []
        total_partner_foreign = Decimal("0")
        total_partner_pkr = Decimal("0")

        for p, share_pct in eligible:
            # Partner gets share_pct% of the TRANSACTION AMOUNT
            amt_foreign = _q(tx_amount_foreign * share_pct / Decimal("100"))
            amt_pkr = _q(tx_amount_pkr * share_pct / Decimal("100"))
            total_partner_foreign += amt_foreign
            total_partner_pkr += amt_pkr

            entry = PartnerLedgerEntry.objects.create(
                partner=p,
                payment=payment,
                share_snapshot=share_pct,
                fee_total_foreign=fee_foreign,
                fee_total_pkr=fee_pkr,
                amount_foreign=amt_foreign,
                amount_pkr=amt_pkr,
                currency_code=payment.currency_id,
            )
            created.append(entry)

        company_retained_foreign = fee_foreign - total_partner_foreign
        company_retained_pkr = fee_pkr - total_partner_pkr

        log.info(
            "Created %d partner ledger entries for %s "
            "(tx=%s %s, fee=%s, partners_total=%s, company_retains=%s)",
            len(created), payment.reference,
            tx_amount_foreign, payment.currency_id,
            fee_foreign, total_partner_foreign, company_retained_foreign,
        )
        return created


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
    Company's profit on a single payment:
        company_profit = fee_collected − sum(partner payouts)

    Returns dict with foreign and PKR amounts.
    """
    from myapp.Models.Partner_models import PartnerLedgerEntry
    from django.db.models import Sum

    fee_foreign = _q(payment.fee_amount_foreign or 0)
    fee_pkr = _q((payment.fee_amount_foreign or 0) * (payment.exchange_rate or 0))

    partner_agg = PartnerLedgerEntry.objects.filter(payment=payment).aggregate(
        total_foreign=Sum("amount_foreign"),
        total_pkr=Sum("amount_pkr"),
    )
    partner_foreign = _q(partner_agg["total_foreign"] or 0)
    partner_pkr = _q(partner_agg["total_pkr"] or 0)

    return {
        "fee_foreign": fee_foreign,
        "fee_pkr": fee_pkr,
        "partner_total_foreign": partner_foreign,
        "partner_total_pkr": partner_pkr,
        "company_foreign": fee_foreign - partner_foreign,
        "company_pkr": fee_pkr - partner_pkr,
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
