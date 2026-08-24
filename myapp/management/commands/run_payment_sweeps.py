"""
Run the two payment-queue sweeps once, synchronously, without Celery.

Normally `flag_stale_payments` and `auto_confirm_stale_payments` are driven
by celery-beat every 30 minutes (see CELERY_BEAT_SCHEDULE). This command is
the same work behind a plain entry point, for two situations:

  1. **Safety net.** If beat dies, nothing auto-approves and payments pile
     up in Awaiting Customer Confirmation with no visible error. Wiring this
     into OS cron as a belt-and-braces hourly run means a dead beat degrades
     into a slightly coarser schedule instead of a silent stall:

         0 * * * * cd /srv/paidix && ./venv/bin/python manage.py run_payment_sweeps

     Running it alongside beat is harmless. Both tasks are idempotent —
     they select by cutoff and re-check status under a row lock — so a
     double run does nothing the first one didn't already do.

  2. **Local dev on Windows**, where Celery's prefork pool doesn't work and
     running a worker + beat is more trouble than it's worth.

    # Just report what the thresholds are and what would be swept.
    python manage.py run_payment_sweeps --dry-run

    # Do the work.
    python manage.py run_payment_sweeps

    # Flag stale payments but don't auto-approve anything.
    python manage.py run_payment_sweeps --skip-auto-confirm
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Run the stale-payment flag and auto-approve sweeps once, without "
        "Celery. Safe to run alongside celery-beat; both sweeps are idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be swept without changing anything.",
        )
        parser.add_argument(
            "--skip-flag", action="store_true",
            help="Don't flag newly-stale payments.",
        )
        parser.add_argument(
            "--skip-auto-confirm", action="store_true",
            help="Don't auto-approve payments past the auto-confirm window.",
        )

    def handle(self, *args, **options):
        from myapp.Models.Transaction_models import (
            IncomingPayment, TransactionStatus,
        )
        from myapp.Utils.stale_payment_tasks import (
            _resolve_auto_confirm_minutes, _resolve_threshold_minutes,
            auto_confirm_stale_payments, flag_stale_payments,
        )

        dry_run = options["dry_run"]
        stale_minutes = _resolve_threshold_minutes()
        auto_minutes = _resolve_auto_confirm_minutes()

        self.stdout.write(
            f"stale threshold      : {stale_minutes} min "
            f"({self._human(stale_minutes)})"
        )
        self.stdout.write(
            "auto-approve window  : "
            + ("disabled" if auto_minutes == 0
               else f"{auto_minutes} min ({self._human(auto_minutes)}) after going stale")
        )
        self.stdout.write("")

        now = timezone.now()

        # ── Sweep 1: flag newly-stale payments ───────────────────────────
        if options["skip_flag"]:
            self.stdout.write("flag sweep           : skipped")
        elif dry_run:
            n = IncomingPayment.objects.filter(
                status=TransactionStatus.PKR_SENT,
                is_stale=False,
                updated_at__lt=now - timedelta(minutes=stale_minutes),
            ).count()
            self.stdout.write(f"flag sweep (dry run) : would flag {n} payment(s)")
        else:
            result = flag_stale_payments()
            self.stdout.write(
                self.style.SUCCESS(
                    f"flag sweep           : flagged {result['flagged']} payment(s)"
                )
            )

        # ── Sweep 2: auto-approve what's past the window ─────────────────
        if options["skip_auto_confirm"]:
            self.stdout.write("auto-approve sweep   : skipped")
        elif auto_minutes == 0:
            self.stdout.write(
                "auto-approve sweep   : disabled "
                "(auto_confirm_payment_minutes = 0)"
            )
        elif dry_run:
            n = IncomingPayment.objects.filter(
                status=TransactionStatus.PKR_SENT,
                is_stale=True,
                stale_at__isnull=False,
                stale_at__lt=now - timedelta(minutes=auto_minutes),
            ).count()
            self.stdout.write(
                f"auto-approve (dry)   : would auto-approve {n} payment(s)"
            )
        else:
            result = auto_confirm_stale_payments()
            self.stdout.write(
                self.style.SUCCESS(
                    f"auto-approve sweep   : approved {result.get('confirmed', 0)} "
                    f"of {result.get('due', 0)} due"
                )
            )

        # ── Where the queue stands afterwards ────────────────────────────
        waiting = IncomingPayment.objects.filter(
            status=TransactionStatus.PKR_SENT,
        ).count()
        in_queue = IncomingPayment.objects.filter(
            status=TransactionStatus.PKR_SENT, is_stale=True,
        ).count()
        self.stdout.write("")
        self.stdout.write(
            f"awaiting confirmation: {waiting} PKR-sent, {in_queue} in the queue"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n(dry run — nothing changed)"))

    @staticmethod
    def _human(minutes):
        days, rem = divmod(max(0, int(minutes)), 24 * 60)
        hours, mins = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if mins:
            parts.append(f"{mins}m")
        return " ".join(parts) or "0m"
