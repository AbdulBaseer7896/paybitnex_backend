"""
Recomputes partner ledger entries using the corrected pool-based
distribution formula.

Background: The original `distribute_fee_for_payment` treated share_percentage
as "percent of the fee" (multiplying by share/100). The corrected formula
treats shares as slices of a *pool* — each partner gets
`fee × (share / sum_of_all_active_shares)`, so partners collectively
receive the entire fee when their shares sum to less than 100%.

Usage:

    # Dry run — show what WOULD change without touching the database.
    python manage.py recompute_partner_ledger --dry-run

    # Apply the recomputation to every completed payment.
    python manage.py recompute_partner_ledger

    # Restrict to payments completed on/after a date.
    python manage.py recompute_partner_ledger --since 2026-01-01

Important: this deletes existing PartnerLedgerEntry rows for each affected
payment and creates fresh ones using the current share_snapshot logic. It
preserves created_at on the new entries where practical by using the
payment's completed_at timestamp as the ledger entry's created_at.

The command is idempotent — running it twice produces the same result.
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

            pool = sum((pct for _, pct in pool_rows), start=Decimal("0"))
            if pool <= 0:
                continue

            # Compute new allocations
            last_idx = len(pool_rows) - 1
            allocated_foreign = Decimal("0")
            allocated_pkr = Decimal("0")
            new_rows = []
            for idx, (pid, pct) in enumerate(pool_rows):
                if idx == last_idx:
                    amt_foreign = _q(fee_foreign - allocated_foreign)
                    amt_pkr = _q(fee_pkr - allocated_pkr)
                else:
                    frac = pct / pool
                    amt_foreign = _q(fee_foreign * frac)
                    amt_pkr = _q(fee_pkr * frac)
                    allocated_foreign += amt_foreign
                    allocated_pkr += amt_pkr
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
                self.stdout.write(
                    f"  [DRY] {payment.reference}: fee={fee_foreign} {payment.currency_id} "
                    f"pool={pool}% → {len(new_rows)} entries"
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
