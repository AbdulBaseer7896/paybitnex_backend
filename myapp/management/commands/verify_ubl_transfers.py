"""
Django management command to reconcile OutgoingPKRTransfer records against UBL bank statements,
and automatically persist all statement records into BankStatementRecord with BankSyncJob audit logs.

Usage:
  # 1. Standard 3-day window dry-run (default: safe, no DB changes)
  python manage.py verify_ubl_transfers

  # 2. Dry-run across all of current month
  python manage.py verify_ubl_transfers --all-month

  # 3. Commit verification results to database
  python manage.py verify_ubl_transfers --commit

  # 4. Revert / Undo a previous verification run
  python manage.py verify_ubl_transfers --undo --all-month --commit
"""

import os
from datetime import datetime, date
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone

from myapp.Services.ubl_reconciliation import (
    reconcile_ubl_transfers,
    get_reconciliation_window,
    get_unverified_transfers_window,
    get_latest_statement_file,
)
from myapp.Services.UBL_scrapper import scrape_ubl_statement
from myapp.Services.ubl_statement_sync import ingest_ubl_statement_file
from myapp.Models.BankAudit_models import BankSyncJob
from myapp.Utils.email_tasks import send_system_alert_email


class Command(BaseCommand):
    help = "Reconciles outgoing PKR transfers against UBL bank statement exports and records statement transactions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            help="Path to UBL statement CSV. Skips scraping and uses this file directly.",
        )
        parser.add_argument(
            "--local",
            action="store_true",
            default=False,
            help="Use the latest locally downloaded statement in Bank_statments without running the scraper.",
        )
        parser.add_argument(
            "--no-scrape",
            dest="local",
            action="store_true",
            help="Alias for --local (skips scraping).",
        )
        parser.add_argument(
            "--keep-statement",
            action="store_true",
            default=False,
            help="Keep the downloaded statement CSV instead of deleting it after reconciliation.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            default=False,
            help="Apply verification results to the database. (Default is dry-run).",
        )
        parser.add_argument(
            "--undo",
            action="store_true",
            default=False,
            help="Roll back verified transfers in the window back to unverified.",
        )
        parser.add_argument(
            "--all-month",
            action="store_true",
            default=False,
            help="Reconcile across the entire current month (e.g. Sept 1 to today).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Number of days in sliding window (e.g. 3). If omitted, defaults to month-to-date up to yesterday.",
        )
        parser.add_argument(
            "--lag",
            type=int,
            default=1,
            help="Gap/lag days from today (default: 1, e.g. on 11th checks 8th-10th).",
        )
        parser.add_argument(
            "--range",
            nargs=2,
            metavar=("START_DATE", "END_DATE"),
            help="Custom date range (e.g. --range 08.09.2026 10.09.2026 or --range 2026-09-08 2026-09-10).",
        )
        parser.add_argument(
            "--start-date",
            type=str,
            help="Custom start date (e.g. 08.09.2026 or 2026-09-08).",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            help="Custom end date (e.g. 10.09.2026 or 2026-09-10).",
        )
        parser.add_argument(
            "--proxy",
            type=str,
            default=None,
            help="Custom proxy string (host:port:user:pass or 'none' to disable). Defaults to configured Pakistan residential proxy.",
        )
        parser.add_argument(
            "--no-proxy",
            action="store_true",
            default=False,
            help="Disable proxy and connect directly to UBL portal without proxy.",
        )
        parser.add_argument(
            "--reverify-all",
            action="store_true",
            default=False,
            help="Re-verify all transfers in the window, including already verified ones.",
        )

    def _send_failure_alert(self, exc, stage="Execution", start_date=None, end_date=None):
        import traceback
        import datetime

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        pkt_tz = datetime.timezone(datetime.timedelta(hours=5))
        now_pkt = now_utc.astimezone(pkt_tz)

        err_msg = str(exc).strip() or type(exc).__name__
        tb_str = traceback.format_exc()

        is_quota = any(
            phrase in (err_msg + " " + tb_str).lower()
            for phrase in [
                "traffic limit",
                "quota",
                "407",
                "proxy authentication",
                "proxy gateway error",
                "restricted target",
                "err_tunnel_connection_failed",
            ]
        )

        if is_quota:
            subject = "[URGENT] UBL Scraper Failed: Decodo Proxy Quota Exceeded"
            action_notice = (
                "ACTION REQUIRED: Decodo residential proxy data plan traffic limit has been reached "
                "or proxy authentication was rejected (407). Please top up / recharge data on gate.decodo.com."
            )
        else:
            subject = f"[ALERT] UBL Payment Verification Failed ({stage}): {type(exc).__name__}"
            action_notice = (
                "ACTION REQUIRED: The automated UBL reconciliation job encountered an error during execution. "
                "Please inspect the server logs at /opt/paybitnex_backend/Bank_statments/cron_ubl.log."
            )

        body_text = f"""=======================================================
   UBL PAYMENT VERIFICATION - FAILURE ALERT
=======================================================

Stage:       {stage}
Status:      FAILED
Time (PKT):  {now_pkt.strftime('%Y-%m-%d %I:%M:%S %p')}
Time (UTC):  {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}
Date Range:  {start_date or 'N/A'} to {end_date or 'N/A'}
Server:      PayBitnex Server

{action_notice}

-------------------------------------------------------
ERROR SUMMARY:
-------------------------------------------------------
{err_msg}

-------------------------------------------------------
FULL TRACEBACK:
-------------------------------------------------------
{tb_str}
"""

        header_bg = "#991b1b" if is_quota else "#1e293b"
        header_title = "⚠️ Decodo Proxy Quota Exceeded" if is_quota else "⚠️ UBL Transfer Verification Failed"
        alert_bg = "#fef2f2" if is_quota else "#fffbeb"
        alert_border = "#dc2626" if is_quota else "#f59e0b"
        alert_color = "#991b1b" if is_quota else "#92400e"

        body_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #f1f5f9; margin: 0; padding: 24px; color: #0f172a; }}
    .card {{ background: #ffffff; border-radius: 8px; max-width: 680px; margin: 0 auto; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
    .header {{ background: {header_bg}; color: #ffffff; padding: 20px 24px; }}
    .header h2 {{ margin: 0; font-size: 20px; font-weight: 700; }}
    .header p {{ margin: 4px 0 0; font-size: 13px; opacity: 0.9; }}
    .content {{ padding: 24px; }}
    .alert-box {{ background: {alert_bg}; border-left: 4px solid {alert_border}; padding: 14px 16px; margin-bottom: 20px; border-radius: 0 6px 6px 0; font-size: 14px; line-height: 1.5; color: {alert_color}; }}
    .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }}
    .meta-table td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }}
    .meta-table td.label {{ font-weight: 600; color: #64748b; width: 130px; }}
    .error-code {{ background: #0f172a; color: #f87171; padding: 14px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 280px; overflow-y: auto; }}
    .footer {{ background: #f8fafc; border-top: 1px solid #e2e8f0; padding: 14px 24px; font-size: 12px; color: #64748b; text-align: center; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h2>{header_title}</h2>
      <p>Automated Portal Scraper &amp; Reconciliation Engine</p>
    </div>
    <div class="content">
      <div class="alert-box">
        <strong>{action_notice}</strong>
      </div>
      <table class="meta-table">
        <tr><td class="label">Stage:</td><td><strong>{stage}</strong></td></tr>
        <tr><td class="label">Time (PKT):</td><td>{now_pkt.strftime('%Y-%m-%d %I:%M:%S %p')}</td></tr>
        <tr><td class="label">Time (UTC):</td><td>{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>
        <tr><td class="label">Date Range:</td><td>{start_date or 'N/A'} to {end_date or 'N/A'}</td></tr>
        <tr><td class="label">Server:</td><td>PayBitnex Server</td></tr>
      </table>
      <div style="font-weight: 600; font-size: 13px; margin-bottom: 6px; color: #334155;">Error Output:</div>
      <div class="error-code">{err_msg}

{tb_str}</div>
    </div>
    <div class="footer">
      PayBitnex Automated Bank Reconciliation System &bull; Recipients: abdulbasirqazi@gmail.com, abdullah.shahid1045@gmail.com
    </div>
  </div>
</body>
</html>"""

        self.stdout.write(self.style.WARNING(f"\n[ALERT] Sending failure notification email to admin recipients..."))
        ok = send_system_alert_email(
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            to=["abdulbasirqazi@gmail.com", "abdullah.shahid1045@gmail.com"],
            sync=True,
        )
        if ok:
            self.stdout.write(self.style.SUCCESS("[ALERT] Failure alert email sent successfully."))
        else:
            self.stderr.write(self.style.ERROR("[ALERT] Failed to dispatch alert email."))

    def handle(self, *args, **options):
        dry_run = not options["commit"]
        is_undo = options["undo"]

        def parse_date_val(d_str):
            if not d_str:
                return None
            d_str = str(d_str).strip()
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y.%m.%d", "%Y/%m/%d"):
                try:
                    return datetime.strptime(d_str, fmt).date()
                except ValueError:
                    continue
            raise ValueError(f"Unable to parse date '{d_str}'. Supported formats: DD.MM.YYYY, YYYY-MM-DD, DD/MM/YYYY, etc.")

        start_date = None
        end_date = None
        if options.get("range"):
            try:
                start_date = parse_date_val(options["range"][0])
                end_date = parse_date_val(options["range"][1])
            except ValueError as e:
                self.stderr.write(self.style.ERROR(f"Invalid --range dates: {e}"))
                return
        else:
            try:
                if options.get("start_date"):
                    start_date = parse_date_val(options["start_date"])
                if options.get("end_date"):
                    end_date = parse_date_val(options["end_date"])
            except ValueError as e:
                self.stderr.write(self.style.ERROR(f"Invalid date: {e}"))
                return

        if options["all_month"] and not start_date:
            today = timezone.localtime(timezone.now()).date()
            start_date = date(today.year, today.month, 1)
            end_date = today

        if not start_date or not end_date:
            if options.get("days"):
                calc_start, calc_end = get_reconciliation_window(
                    window_days=options["days"],
                    lag_days=options["lag"],
                )
            else:
                calc_start, calc_end, _ = get_unverified_transfers_window(
                    lag_days=options["lag"],
                    current_month_only=not options.get("all_month"),
                )
            start_date = start_date or calc_start
            end_date = end_date or calc_end

        mode_str = "UNDO / ROLLBACK" if is_undo else "RECONCILIATION"
        status_str = "DRY RUN (simulation only)" if dry_run else "LIVE COMMIT (writing to database)"

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n======================================================="))
        self.stdout.write(self.style.MIGRATE_HEADING(f"   UBL PAYMENT VERIFICATION ENGINE - {mode_str}"))
        self.stdout.write(self.style.MIGRATE_HEADING(f"======================================================="))
        self.stdout.write(f"Mode:       {self.style.WARNING(status_str)}")
        self.stdout.write(f"Date Range: {start_date} to {end_date}")

        statement_file = options.get("file")
        downloaded_temp_path = None

        if not is_undo:
            if statement_file:
                self.stdout.write(f"Statement Source: Explicit file ({statement_file})")
            elif options.get("local"):
                statement_file = get_latest_statement_file()
                self.stdout.write(f"Statement Source: Using latest local statement in Bank_statments folder (--local): {statement_file}")
            else:
                self.stdout.write(self.style.MIGRATE_HEADING("\n>>> Running automated UBL portal scraper..."))
                try:
                    from_date_str = start_date.strftime("%d/%m/%Y")
                    to_date_str = end_date.strftime("%d/%m/%Y")
                    active_proxy_arg = False if options.get("no_proxy") else options.get("proxy")
                    downloaded_temp_path = scrape_ubl_statement(
                        from_date=from_date_str,
                        to_date=to_date_str,
                        log_callback=self.stdout.write,
                        proxy=active_proxy_arg,
                    )
                    statement_file = str(downloaded_temp_path)
                    self.stdout.write(self.style.SUCCESS(f">>> Scraper completed successfully: {downloaded_temp_path.name}\n"))
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"\n[FATAL] UBL scraper execution failed: {e}"))
                    self._send_failure_alert(e, stage="UBL Portal Scraping", start_date=start_date, end_date=end_date)
                    return

        # Bank statement ingestion into BankStatementRecord & BankSyncJob
        if statement_file and os.path.exists(statement_file) and not is_undo:
            sync_source = "cron" if not options.get("local") and not options.get("file") else "cli"
            sync_job = BankSyncJob.objects.create(source=sync_source, status="running")
            try:
                ingest_res = ingest_ubl_statement_file(statement_file, sync_job=sync_job)
                sync_job.status = "completed"
                sync_job.completed_at = timezone.now()
                sync_job.save(update_fields=["status", "completed_at"])
                self.stdout.write(self.style.SUCCESS(
                    f"\n>>> Bank Statement Sync: {ingest_res['newly_inserted']} new records saved "
                    f"(+{ingest_res['credits_inserted']} credits, +{ingest_res['debits_inserted']} debits, "
                    f"{ingest_res['skipped_duplicates']} duplicates skipped)."
                ))
            except Exception as ie:
                sync_job.status = "failed"
                sync_job.completed_at = timezone.now()
                sync_job.error_message = str(ie)
                sync_job.save(update_fields=["status", "completed_at", "error_message"])
                self.stderr.write(self.style.WARNING(f"[WARN] Statement records ingestion failed: {ie}"))

        try:
            res = reconcile_ubl_transfers(
                file_path=statement_file,
                start_date=start_date,
                end_date=end_date,
                dry_run=dry_run,
                undo=is_undo,
                source="manual_cli",
                only_unverified=not options["reverify_all"],
            )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"\n[FATAL] UBL reconciliation failed: {e}"))
            self._send_failure_alert(e, stage="Bank Reconciliation", start_date=start_date, end_date=end_date)
            return

        if is_undo:
            self.stdout.write(self.style.SUCCESS(f"\n{res['message']}"))
            return

        self.stdout.write(f"Statement:  {res['statement_file']}")
        self.stdout.write(f"Debits:     {res['total_bank_debits']} outflows parsed from statement\n")

        self.stdout.write(self.style.MIGRATE_HEADING("-------------------- SUMMARY --------------------"))
        tot_win = res.get("total_in_window", res["total_transfers"])
        s_ver = res.get("skipped_verified", 0)
        s_flag = res.get("skipped_flagged", 0)
        if s_ver > 0 or s_flag > 0:
            self.stdout.write(f"Total Transfers in Window: {tot_win}")
            self.stdout.write(self.style.NOTICE(f"  [SKIP] Untouched (Skipped):      {s_ver + s_flag} ({s_ver} already verified, {s_flag} flagged for review)"))
            self.stdout.write(f"  [EVAL] Unverified to Evaluate:   {res['total_transfers']}")
        else:
            self.stdout.write(f"Total Transfers in Window: {res['total_transfers']}")
        self.stdout.write(self.style.SUCCESS(f"  [OK]   Verified / Golden Match:  {res['golden_matches_count']}"))
        self.stdout.write(self.style.WARNING(f"  [FLAG] Flagged Discrepancies:     {res['discrepancies_count']}"))
        self.stdout.write(self.style.ERROR(f"  [MISS] Unmatched (Not in Bank):  {res['unmatched_count']}"))

        def _format_transfer_meta(t):
            cust_name = "N/A"
            if t.customer_bank_account and t.customer_bank_account.customer:
                c = t.customer_bank_account.customer
                cust_name = getattr(c, "full_name", "") or c.email

            if t.incoming_payment:
                linked_str = f"Payment: {t.incoming_payment.reference}"
            else:
                linked_payments = list(t.payments.all())
                if linked_payments:
                    refs = [p.reference for p in linked_payments]
                    if len(refs) > 3:
                        linked_str = f"Bulk ({len(refs)}): {', '.join(refs[:3])}..."
                    else:
                        linked_str = f"Bulk ({len(refs)}): {', '.join(refs)}"
                else:
                    linked_str = "No payment linked"

            return cust_name, linked_str

        if res["golden_matches"]:
            self.stdout.write(self.style.SUCCESS(f"\n[OK] VERIFIED / GOLDEN MATCHES ({len(res['golden_matches'])}):"))
            for m in res["golden_matches"]:
                t = m["transfer"]
                b = m["bank_record"]
                cust_name, linked = _format_transfer_meta(t)
                holder = t.customer_bank_account.holder_name if t.customer_bank_account else "N/A"
                bank_name = t.customer_bank_account.bank.name if (t.customer_bank_account and t.customer_bank_account.bank) else "N/A"
                self.stdout.write(
                    f"  * {t.reference} | PKR {t.amount_pkr:,.2f} | Tx Date: {t.transfer_date} | "
                    f"Customer: '{cust_name}' | Holder: {holder} ({bank_name}) | [{linked}] => Bank Recipient: '{b['BENEFICIARY_NAME']}' on {b['PARSED_DATE']}"
                )

        if res["discrepancies"]:
            self.stdout.write(self.style.WARNING(f"\n[FLAG] FLAGGED DISCREPANCIES ({len(res['discrepancies'])}):"))
            for m in res["discrepancies"]:
                t = m["transfer"]
                b = m["bank_record"]
                cust_name, linked = _format_transfer_meta(t)
                holder = t.customer_bank_account.holder_name if t.customer_bank_account else "N/A"
                self.stdout.write(
                    f"  * {t.reference} | PKR {t.amount_pkr:,.2f} | Customer: '{cust_name}' | Holder: '{holder}' vs Bank: '{b['BENEFICIARY_NAME']}' on {b['PARSED_DATE']} | [{linked}]"
                )
                self.stdout.write(f"    Note: {m['notes']}")

        if res["unmatched"]:
            self.stdout.write(self.style.ERROR(f"\n[MISS] UNMATCHED IN BANK STATEMENT ({len(res['unmatched'])}):"))
            for m in res["unmatched"]:
                t = m["transfer"]
                cust_name, linked = _format_transfer_meta(t)
                holder = t.customer_bank_account.holder_name if t.customer_bank_account else "N/A"
                self.stdout.write(
                    f"  * {t.reference} | PKR {t.amount_pkr:,.2f} | Date: {t.transfer_date} | Customer: '{cust_name}' | Holder: {holder} | [{linked}]"
                )

        self.stdout.write("\n" + "=" * 55)
        if dry_run:
            self.stdout.write(
                self.style.NOTICE(
                    "Dry run complete. No database changes were made.\n"
                    "To apply these verification statuses, run again with: --commit"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Commit complete! Database updated successfully.\n"
                    "To revert this run if needed, use: --undo --commit"
                )
            )

        if downloaded_temp_path and os.path.exists(downloaded_temp_path):
            if not options.get("keep_statement"):
                try:
                    os.remove(downloaded_temp_path)
                    self.stdout.write(self.style.NOTICE(f"[CLEANUP] Deleted downloaded temporary statement: {downloaded_temp_path.name}"))
                except Exception as e:
                    self.stderr.write(self.style.WARNING(f"[CLEANUP] Could not delete {downloaded_temp_path.name}: {e}"))
            else:
                self.stdout.write(f"[KEPT] Downloaded statement preserved: {downloaded_temp_path}")

        self.stdout.write("=" * 55 + "\n")
