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

    Idempotent: if entries already exist for this payment, does nothing.
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

        created = []
        for p in partners:
            share = getattr(p, "share", None)
            if share is None or share.percentage <= 0:
                continue
            share_pct = share.percentage
            share_frac = share_pct / Decimal("100")

            amt_foreign = _q(fee_foreign * share_frac)
            amt_pkr = _q(fee_pkr * share_frac)

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
            "Created %d partner ledger entries for %s (fee=%s %s)",
            len(created), payment.reference, fee_foreign, payment.currency_id,
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
