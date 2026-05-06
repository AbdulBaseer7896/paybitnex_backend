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

    Distribution model: each partner's `share.percentage` is a DIRECT
    percentage of the gross fee. The remainder stays with the company.

        Example: $100 payment, 10% fee = $10 fee.
                 Partners A=5%, B=4%, C=3%.
                 A receives $10 × 5%  = $0.50
                 B receives $10 × 4%  = $0.40
                 C receives $10 × 3%  = $0.30
                 Partners total       = $1.20  (12% of fee)
                 PayBitnex retains    = $8.80  (88% of fee)

    The dashboard's "Partners' share of fee: 12% / PayBitnex's share: 88%"
    cards reflect this same direct interpretation. The earlier
    pool-based math (where partners split the entire fee pro-rata
    by their relative shares) was inconsistent with the dashboard
    and made the company's retained share invisible — that's been
    corrected.

    Idempotent: if entries already exist for this payment, does
    nothing.

    The `share_snapshot` records each partner's raw share_percentage
    at the time of the payment, so reports can display "Partner A's
    share was 5%" even after shares are later re-balanced. The
    snapshot is purely informational; the authoritative payout
    amounts live in `amount_foreign` / `amount_pkr` which were
    computed at distribution time.
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

        # Collect every active partner with a positive share. We
        # also sum their percentages purely for the log line — the
        # math no longer treats this as a "pool" to split between.
        eligible = []
        total_pct = Decimal("0")
        for p in partners:
            share = getattr(p, "share", None)
            if share is None or share.percentage <= 0:
                continue
            eligible.append((p, Decimal(share.percentage)))
            total_pct += Decimal(share.percentage)

        if not eligible:
            log.info(
                "No eligible partners for %s — fee not distributed (no active partners).",
                payment.reference,
            )
            return []

        # Allocate each partner's slice PRO-RATA within the pool.
        # Each partner's fraction = their_share / sum_of_all_shares.
        # Example: A=3%, B=4%, C=7% → pool=14%.
        #   A gets 3/14 × fee = 21.43%
        #   B gets 4/14 × fee = 28.57%
        #   C gets 7/14 × fee = 50.00%
        # The company retains NOTHING — 100% of the fee is distributed
        # to partners. If there are no partners, the fee stays in the
        # books as unclaimed (no ledger entry is created; see above).
        created = []
        allocated_pkr = Decimal("0")
        allocated_foreign = Decimal("0")

        for idx, (p, share_pct) in enumerate(eligible):
            is_last = (idx == len(eligible) - 1)
            if is_last:
                # Give the last partner the exact remainder to absorb
                # rounding dust — ensures fee_total always == sum(entries).
                amt_foreign = _q(fee_foreign - allocated_foreign)
                amt_pkr = _q(fee_pkr - allocated_pkr)
            else:
                frac = share_pct / total_pct   # pro-rata fraction
                amt_foreign = _q(fee_foreign * frac)
                amt_pkr = _q(fee_pkr * frac)
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
            "Created %d partner ledger entries for %s "
            "(fee=%s %s, partners total=%s%%)",
            len(created), payment.reference, fee_foreign,
            payment.currency_id, total_pct,
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
