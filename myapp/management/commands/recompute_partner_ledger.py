"""
Recomputes partner ledger entries using the corrected DIRECT-share
distribution formula.

Background: an early version of `distribute_fee_for_payment`
normalized partner shares against the sum of all active shares —
so partners with 13% and 10% would split the entire fee 56.5/43.5
between them, instead of receiving 13% and 10% of the fee with
the remaining 77% going to the company.

The corrected formula treats each `share_percentage` as a DIRECT
percentage of the gross fee:

    fee = $100. Partners A=13%, B=10%.
    A receives $100 × 13% = $13.00
    B receives $100 × 10% = $10.00
    Company retains       = $77.00

This matches the "PayBitnex's share of fee" card on the dashboard
and is the formula `distribute_fee_for_payment` now uses for new
payments.

Run this command after deploying that fix to recompute every
historical payment's ledger entries:

    # Dry run — show what WOULD change without touching the database.
    python manage.py recompute_partner_ledger --dry-run

    # Apply the recomputation to every completed payment.
    python manage.py recompute_partner_ledger

    # Restrict to payments completed on/after a date.
    python manage.py recompute_partner_ledger --since 2026-01-01

The command deletes existing PartnerLedgerEntry rows for each
affected payment and creates fresh ones using the direct-share
formula. It preserves the original share_snapshot values where
they exist so historical "Partner A's share was 13%" labels stay
correct.

Idempotent — running it twice produces the same result.
"""
from decimal import Decimal, ROUND_HALF_UP
from django.core.management.base import BaseCommand
from django.db import transaction as dbtx

from myapp.Models.Partner_models import Partner, PartnerLedgerEntry
from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus

ZERO = Decimal("0.00")
QUANT = Decimal("0.01")


def _q(x):
    return Decimal(x).quantize(QUANT, rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = "Recompute partner ledger entries with the pool-based distribution formula."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be recomputed without writing to the database.",
        )
        parser.add_argument(
            "--since", default=None,
            help="Only recompute payments completed on/after this ISO date (YYYY-MM-DD).",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        since = opts.get("since")

        # Include both COMPLETED and PKR_SENT — fee is finalized at either
        # status, and if distribution happened too eagerly it may have
        # created ledger entries on PKR_SENT transactions as well.
        qs = IncomingPayment.objects.filter(
            status__in=[TransactionStatus.COMPLETED, TransactionStatus.PKR_SENT],
        ).exclude(fee_amount_foreign__isnull=True).exclude(exchange_rate__isnull=True)

        if since:
            # completed_at may be null for PKR_SENT rows; fall back to
            # created_at when filtering by date to avoid skipping valid rows.
            from django.db.models import Q
            qs = qs.filter(
                Q(completed_at__gte=since) | Q(created_at__gte=since),
            )

        qs = qs.order_by("created_at")
        total = qs.count()
        self.stdout.write(f"Found {total} completed payments with fee+rate set.")

        if total == 0:
            return

        # Use the currently-active partners' shares as the snapshot pool.
        # (The OLD ledger entries contain historical share_snapshot values
        # that we try to preserve; but for payments that lost entries
        # we fall back to today's active shares.)
        active_partners = list(
            Partner.objects.filter(is_active=True).select_related("share"),
        )

        changed = 0
        for i, payment in enumerate(qs, 1):
            fee_foreign = _q(payment.fee_amount_foreign)
            fee_pkr = _q(fee_foreign * payment.exchange_rate)

            existing = list(PartnerLedgerEntry.objects.filter(payment=payment))

            # Re-use the partner+snapshot pairs from the existing entries so
            # historical partner allocations remain consistent. If none
            # exist (e.g. distribution never ran), fall back to current
            # active partners.
            if existing:
                pool_rows = [
                    (e.partner_id, Decimal(e.share_snapshot))
                    for e in existing if Decimal(e.share_snapshot) > 0
                ]
            else:
                pool_rows = [
                    (p.id, Decimal(p.share.percentage))
                    for p in active_partners
                    if getattr(p, "share", None) and p.share.percentage > 0
                ]

            if not pool_rows:
                continue

            # Each share_snapshot is a percent value (e.g. 13 means 13%).
            # Direct interpretation: a partner with share=13 gets 13%
            # of the fee. The remainder stays with the company.
            # (Earlier this code normalized shares to a "pool sum"
            # so partners between them split 100% of the fee — that
            # was inconsistent with the dashboard's "PayBitnex's
            # share" card and with how `distribute_fee_for_payment`
            # now works.)
            new_rows = []
            for pid, pct in pool_rows:
                share_frac = pct / Decimal("100")
                amt_foreign = _q(fee_foreign * share_frac)
                amt_pkr = _q(fee_pkr * share_frac)
                new_rows.append((pid, pct, amt_foreign, amt_pkr))

            # Compare against existing
            needs_update = False
            existing_map = {e.partner_id: e for e in existing}
            for pid, pct, amt_f, amt_p in new_rows:
                ex = existing_map.get(pid)
                if not ex:
                    needs_update = True
                    break
                if (Decimal(ex.amount_foreign) != amt_f
                        or Decimal(ex.amount_pkr) != amt_p):
                    needs_update = True
                    break

            if not needs_update:
                continue

            changed += 1
            if dry:
                # Sum of share percentages across all eligible
                # partners on this payment — the company keeps
                # `100 - total_pct` percent of the fee.
                total_pct = sum((pct for _, pct in pool_rows), Decimal("0"))
                self.stdout.write(
                    f"  [DRY] {payment.reference}: fee={fee_foreign} {payment.currency_id} "
                    f"partners total={total_pct}% → {len(new_rows)} entries"
                )
                for pid, pct, amt_f, amt_p in new_rows:
                    old = existing_map.get(pid)
                    old_str = (f"was {old.amount_foreign}"
                               if old else "NEW")
                    self.stdout.write(
                        f"      partner={pid} share={pct}%  "
                        f"→ {amt_f} {payment.currency_id} / {amt_p} PKR ({old_str})"
                    )
                continue

            with dbtx.atomic():
                PartnerLedgerEntry.objects.filter(payment=payment).delete()
                for pid, pct, amt_f, amt_p in new_rows:
                    PartnerLedgerEntry.objects.create(
                        partner_id=pid,
                        payment=payment,
                        share_snapshot=pct,
                        fee_total_foreign=fee_foreign,
                        fee_total_pkr=fee_pkr,
                        amount_foreign=amt_f,
                        amount_pkr=amt_p,
                        currency_code=payment.currency_id,
                    )

            if i % 50 == 0:
                self.stdout.write(f"  processed {i}/{total} payments…")

        if dry:
            self.stdout.write(self.style.WARNING(
                f"Dry run complete. {changed} payments WOULD be recomputed."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. Recomputed ledger entries for {changed} payments."
            ))
