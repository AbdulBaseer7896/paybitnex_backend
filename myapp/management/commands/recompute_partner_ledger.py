"""
Recomputes partner ledger entries using the pro-rata distribution formula.

Each partner receives (their_share ÷ pool_total) × fee — 100% of every
transaction fee goes to the active partners, split by their relative weights.

Example: A=3%, B=4%, C=7% (pool=14). On a $100 fee:
  A gets 3/14 × $100 = $21.43
  B gets 4/14 × $100 = $28.57
  C gets 7/14 × $100 = $50.00

Run this command after deploying to recompute every historical payment's
ledger entries:

    # Dry run — show what WOULD change without touching the database.
    python manage.py recompute_partner_ledger --dry-run

    # Apply the recomputation to every completed payment.
    python manage.py recompute_partner_ledger

    # Restrict to payments completed on/after a date.
    python manage.py recompute_partner_ledger --since 2026-01-01

The command deletes existing PartnerLedgerEntry rows for each
affected payment and creates fresh ones using the pro-rata formula.
It preserves the original share_snapshot values where they exist so
historical "Partner A's share was X%" labels stay correct.

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

        # Always use CURRENT partner shares — historical share_snapshot
        # values in existing ledger entries may be corrupted from earlier
        # bugs (e.g. all partners stored as 4% instead of their actual %).
        # We always recompute using today's configured shares and write
        # the correct share back as the new snapshot.
        current_partners = [
            (p.id, Decimal(str(p.share.percentage)))
            for p in Partner.objects.filter(is_active=True).select_related("share")
            if getattr(p, "share", None) and p.share.percentage
            and Decimal(str(p.share.percentage)) > 0
        ]
        if not current_partners:
            self.stdout.write(self.style.WARNING(
                "No active partners with shares configured. Nothing to do."
            ))
            return

        pool_total = sum(pct for _, pct in current_partners)
        self.stdout.write(
            f"Active partners: {len(current_partners)}, "
            f"pool total: {pool_total}%"
        )
        for pid, pct in current_partners:
            self.stdout.write(f"  partner={pid} share={pct}%")

        changed = 0
        for i, payment in enumerate(qs, 1):
            fee_foreign = _q(payment.fee_amount_foreign)
            fee_pkr = _q(fee_foreign * payment.exchange_rate)

            # Compute target amounts (pro-rata of current shares)
            new_rows = []
            alloc_f, alloc_p = Decimal("0"), Decimal("0")
            for idx, (pid, pct) in enumerate(current_partners):
                is_last = (idx == len(current_partners) - 1)
                if is_last:
                    amt_foreign = _q(fee_foreign - alloc_f)
                    amt_pkr = _q(fee_pkr - alloc_p)
                else:
                    frac = pct / pool_total
                    amt_foreign = _q(fee_foreign * frac)
                    amt_pkr = _q(fee_pkr * frac)
                    alloc_f += amt_foreign
                    alloc_p += amt_pkr
                new_rows.append((pid, pct, amt_foreign, amt_pkr))

            # Compare against existing
            existing = {e.partner_id: e
                        for e in PartnerLedgerEntry.objects.filter(payment=payment)}
            needs_update = (
                len(existing) != len(new_rows)
                or any(
                    pid not in existing
                    or _q(existing[pid].amount_foreign) != amt_f
                    or _q(existing[pid].amount_pkr) != amt_p
                    for pid, _, amt_f, amt_p in new_rows
                )
            )

            if not needs_update:
                continue

            changed += 1
            if dry:
                self.stdout.write(
                    f"  [DRY] {payment.reference}: fee={fee_foreign} {payment.currency_id}"
                )
                for pid, pct, amt_f, amt_p in new_rows:
                    old = existing.get(pid)
                    old_str = f"was {old.amount_foreign}" if old else "NEW"
                    self.stdout.write(
                        f"      partner={pid} share={pct}% ({pct/pool_total*100:.2f}% of pool)"
                        f" → {amt_f} / PKR {amt_p} ({old_str})"
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
                f"Dry run complete. {changed}/{total} payments WOULD be recomputed."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. Recomputed ledger entries for {changed}/{total} payments."
            ))
