"""
Partner ledger service — the heart of profit distribution.

When a transaction is verified & completed, this service creates
PartnerLedgerEntry rows for every active partner, snapshotting
their share % so historical reports stay immutable.
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as dbtx

log = logging.getLogger(__name__)
ZERO = Decimal("0.00")
QUANT = Decimal("0.01")


def _q(x):
    return Decimal(x).quantize(QUANT, rounding=ROUND_HALF_UP)


def distribute_fee_for_payment(payment) -> list:
    """
    Create PartnerLedgerEntry rows for the given IncomingPayment.
    Returns the list of created entries.

    Distribution model: the fee IS the profit pool, and the partners SHARE
    that entire pool pro-rata by their share_percentage.

        Example: $100 payment, 10% fee = $10 profit.
                 Partners A=5%, B=4%, C=3% (pool = 12%).
                 A receives $10 × (5/12) = $4.17
                 B receives $10 × (4/12) = $3.33
                 C receives $10 × (3/12) = $2.50
                 Total distributed = $10.00 (entire fee).

    Historically this code multiplied by `share_percentage / 100`, which
    interpreted shares as "percent of the fee" rather than "percent of the
    profit pool". For the example above that incorrectly produced
    A=$0.50, B=$0.40, C=$0.30 — leaving $8.80 of the fee unaccounted for.
    The pool-based calculation here is the business rule Bitnex confirmed.

    Idempotent: if entries already exist for this payment, does nothing.

    The `share_snapshot` still records each partner's raw share_percentage
    at the time of the payment, so reports can display "Partner A's share
    was 5%" even after shares are later re-balanced. The stored snapshot
    is NOT used as a direct multiplier on the fee during reporting — the
    snapshot is purely informational; the authoritative payout is in
    `amount_foreign` / `amount_pkr` which were computed at distribution
    time using the pool-based math.
    """
    from myapp.Models.Partner_models import Partner, PartnerLedgerEntry

    if payment.fee_amount_foreign is None or payment.exchange_rate is None:
        raise ValueError("Payment must have fee_amount_foreign and exchange_rate set.")

    with dbtx.atomic():
        existing = PartnerLedgerEntry.objects.filter(payment=payment).exists()
        if existing:
            log.info("Ledger entries already exist for %s — skipping.", payment.reference)
            return list(PartnerLedgerEntry.objects.filter(payment=payment))

        fee_foreign = _q(payment.fee_amount_foreign)
        fee_pkr = _q(fee_foreign * payment.exchange_rate)

        partners = list(
            Partner.objects.filter(is_active=True)
            .select_related("share")
        )

        # First pass: collect every active partner with a positive share,
        # and compute the total "pool" (sum of all active shares). If no
        # partners have a share we short-circuit — the company retains
        # the entire fee and there are no ledger entries to create.
        eligible = []
        pool = Decimal("0")
        for p in partners:
            share = getattr(p, "share", None)
            if share is None or share.percentage <= 0:
                continue
            eligible.append((p, Decimal(share.percentage)))
            pool += Decimal(share.percentage)

        if not eligible or pool <= 0:
            log.info(
                "No eligible partners for %s — full fee retained by PayBitnex.",
                payment.reference,
            )
            return []

        # Second pass: allocate each partner's pro-rata slice of the fee
        # relative to the pool. We track the running total and assign the
        # remainder to the last partner, which prevents cumulative rounding
        # error from leaving a few paisa unaccounted for (or over-allocated).
        created = []
        allocated_foreign = Decimal("0")
        allocated_pkr = Decimal("0")
        last_idx = len(eligible) - 1

        for i, (p, share_pct) in enumerate(eligible):
            if i == last_idx:
                # Final partner takes whatever's left so the sum is exact.
                amt_foreign = _q(fee_foreign - allocated_foreign)
                amt_pkr = _q(fee_pkr - allocated_pkr)
            else:
                share_frac = share_pct / pool
                amt_foreign = _q(fee_foreign * share_frac)
                amt_pkr = _q(fee_pkr * share_frac)
                allocated_foreign += amt_foreign
                allocated_pkr += amt_pkr

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

        log.info(
            "Created %d partner ledger entries for %s (fee=%s %s, pool=%s%%)",
            len(created), payment.reference, fee_foreign, payment.currency_id, pool,
        )
        return created


async def adistribute_fee_for_payment(payment) -> list:
    """Async wrapper. Runs the sync function in a thread to preserve atomicity."""
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
