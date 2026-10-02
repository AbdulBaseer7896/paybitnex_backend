"""
Bank-reconciliation audit views (admin only).

Endpoints (mounted under /api/v1/bank-audit/):
  POST   run/                 → run an ad-hoc reconciliation (no save).
  GET    audits/              → list saved audits (history).
  POST   audits/              → save an audit.
  GET    audits/<id>/         → saved-audit detail.
  DELETE audits/<id>/         → delete a saved audit.
  GET    audits/<id>/download/ → download the stored statement file.

UBL Bank Statement Ingestion & Sync Audit (Admin Only):
  GET    ubl-records/         → list & filter parsed UBL statement records with live totals.
  GET    sync-jobs/           → list bank statement scraper/sync execution audit logs.

UBL Auto Payment Verification (Admin Only — replaces cron-triggered run):
  POST   ubl-verify/          → manually trigger the full UBL verify pipeline
                                 (scrape → ingest statement → reconcile transfers).
                                 Equivalent to running:
                                   manage.py verify_ubl_transfers --no-proxy --commit
"""
import os
import threading
from datetime import datetime, date
from decimal import Decimal

from django.db.models import Q, Sum, Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from myapp.Models.Audit_models import AuditLog
from myapp.Models.BankAudit_models import BankAudit, BankAuditFile, BankStatementRecord, BankSyncJob
from myapp.serializers.BankAudit_serializers import (
    BankAuditListSerializer, BankAuditDetailSerializer,
    BankStatementRecordSerializer, BankSyncJobSerializer,
)
from myapp.Utils.bank_audit import run_audit
from myapp.Utils.permissions import IsAdmin


VALID_BANKS = {c[0] for c in BankAudit.BANK_CHOICES}


def _parse_date_param(value):
    """Parse a YYYY-MM-DD query/body value into a date, or None."""
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _client_meta(request):
    ip = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = ip.split(",")[0].strip() if ip else request.META.get("REMOTE_ADDR")
    ua = request.META.get("HTTP_USER_AGENT", "")[:300]
    return ip, ua


class BankAuditRunView(APIView):
    """Run an ad-hoc reconciliation without persisting anything."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        bank = (request.data.get("bank") or "").strip()
        if bank not in VALID_BANKS:
            return Response(
                {"detail": f"Unknown bank '{bank}'. "
                           f"Choose one of: {', '.join(sorted(VALID_BANKS))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "Please attach the bank statement file (CSV or Excel)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start = _parse_date_param(request.data.get("start"))
        end = _parse_date_param(request.data.get("end"))

        try:
            result = run_audit(
                bank, upload, filename=getattr(upload, "name", ""),
                start=start, end=end,
            )
        except ValueError as e:
            return Response({"detail": str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"detail": f"Could not process the file: {e}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "bank": bank,
            "period_start": start.isoformat() if start else None,
            "period_end": end.isoformat() if end else None,
            "result": result,
        })


class BankAuditViewSet(viewsets.ModelViewSet):
    """CRUD for SAVED audits + the statement download action."""
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    queryset = BankAudit.objects.all().prefetch_related("files")
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_serializer_class(self):
        if self.action == "list":
            return BankAuditListSerializer
        return BankAuditDetailSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        bank = self.request.query_params.get("bank")
        if bank:
            qs = qs.filter(bank=bank)
        return qs

    def create(self, request, *args, **kwargs):
        bank = (request.data.get("bank") or "").strip()
        if bank not in VALID_BANKS:
            return Response(
                {"detail": f"Unknown bank '{bank}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = (request.data.get("title") or "").strip()
        if not title:
            return Response(
                {"detail": "Please give this audit a title before saving."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "The statement file is required to save an audit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        start = _parse_date_param(request.data.get("start"))
        end = _parse_date_param(request.data.get("end"))
        notes = (request.data.get("notes") or "").strip()

        try:
            result = run_audit(
                bank, upload, filename=getattr(upload, "name", ""),
                start=start, end=end,
            )
        except ValueError as e:
            return Response({"detail": str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response(
                {"detail": f"Could not process the file: {e}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        summary = result.get("summary", {})
        audit = BankAudit.objects.create(
            title=title,
            bank=bank,
            period_start=start,
            period_end=end,
            total_statement=summary.get("total_statement", 0),
            total_system=summary.get("total_system", 0),
            matched_count=summary.get("matched", 0),
            amount_mismatch_count=summary.get("amount_mismatch", 0),
            only_in_statement_count=summary.get("only_in_statement", 0),
            only_in_system_count=summary.get("only_in_system", 0),
            result=result,
            notes=notes,
            created_by=request.user,
        )

        try:
            upload.seek(0)
        except Exception:
            pass
        BankAuditFile.objects.create(
            audit=audit,
            file=upload,
            original_name=getattr(upload, "name", "")[:255],
            content_type=getattr(upload, "content_type", "") or "",
            size_bytes=getattr(upload, "size", 0) or 0,
        )

        ip, ua = _client_meta(request)
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_CREATE,
            target=audit, target_label=audit.title,
            description=f"Saved bank audit: {audit.title} ({audit.get_bank_display()})",
            metadata={
                "bank": bank,
                "matched": summary.get("matched", 0),
                "amount_mismatch": summary.get("amount_mismatch", 0),
                "only_in_statement": summary.get("only_in_statement", 0),
                "only_in_system": summary.get("only_in_system", 0),
            },
            ip=ip, ua=ua,
        )

        ser = BankAuditDetailSerializer(audit, context={"request": request})
        return Response(ser.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        label = instance.title
        ip, ua = _client_meta(self.request)
        for f in instance.files.all():
            try:
                f.file.delete(save=False)
            except Exception:
                pass
        super().perform_destroy(instance)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_DELETE,
            target_label=label,
            description=f"Deleted bank audit: {label}",
            ip=ip, ua=ua,
        )

    @action(detail=True, methods=["get"], url_path="download")
    def download(self, request, pk=None):
        audit = self.get_object()
        f = audit.files.first()
        if not f or not f.file:
            raise Http404("No statement file is stored for this audit.")
        try:
            fh = f.file.open("rb")
        except Exception:
            raise Http404("The statement file could not be opened.")
        resp = FileResponse(
            fh,
            as_attachment=True,
            filename=f.original_name or "statement.csv",
        )
        return resp


class BankStatementRecordListView(APIView):
    """List and filter UBL bank statement records (Strictly Admin Only)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = BankStatementRecord.objects.all()

        date_from = _parse_date_param(request.query_params.get("date_from"))
        date_to = _parse_date_param(request.query_params.get("date_to"))
        if date_from:
            qs = qs.filter(tran_date__gte=date_from)
        if date_to:
            qs = qs.filter(tran_date__lte=date_to)

        cr_dr = (request.query_params.get("cr_dr") or "").strip().upper()
        if cr_dr in ("C", "D"):
            qs = qs.filter(cr_dr=cr_dr)
        elif "CREDIT" in cr_dr:
            qs = qs.filter(cr_dr__in=["C", "CR"])
        elif "DEBIT" in cr_dr:
            qs = qs.filter(cr_dr__in=["D", "DR"])

        account_number = (request.query_params.get("account_number") or "").strip()
        if account_number:
            qs = qs.filter(account_number__icontains=account_number)

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(tran_ref__icontains=q) |
                Q(channel_ref__icontains=q) |
                Q(tran_desc__icontains=q) |
                Q(tran_desc2__icontains=q) |
                Q(tran_type__icontains=q)
            )

        # Compute summary metrics over the filtered set
        credits_qs = qs.filter(cr_dr__in=["C", "CR"])
        debits_qs = qs.filter(cr_dr__in=["D", "DR"])

        credit_sum = credits_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        debit_sum = debits_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        credit_count = credits_qs.count()
        debit_count = debits_qs.count()
        total_count = qs.count()

        summary = {
            "total_count": total_count,
            "credit_count": credit_count,
            "debit_count": debit_count,
            "total_credit_amount": str(credit_sum),
            "total_debit_amount": str(debit_sum),
            "net_flow": str(credit_sum - debit_sum),
        }

        # Pagination
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = min(200, max(10, int(request.query_params.get("page_size", 50))))
        except (TypeError, ValueError):
            page_size = 50

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_records = list(qs[start_idx:end_idx])

        ser = BankStatementRecordSerializer(page_records, many=True)
        return Response({
            "summary": summary,
            "results": ser.data,
            "count": total_count,
            "page": page,
            "page_size": page_size,
        })


class BankSyncJobListView(APIView):
    """List recent bank statement sync/scraper jobs (Strictly Admin Only)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = BankSyncJob.objects.all()[:50]
        ser = BankSyncJobSerializer(qs, many=True)
        return Response(ser.data)


def _run_ubl_verify_background(job_id, data, user_id, ip, ua):
    """Executes the full UBL scrape, ingest, and reconcile pipeline in a background thread."""
    from django.db import close_old_connections
    close_old_connections()
    try:
        job = BankSyncJob.objects.get(id=job_id)
    except BankSyncJob.DoesNotExist:
        return

    logs = list(job.logs or [])

    def log(msg):
        msg_str = str(msg)
        logs.append(msg_str)
        print(f"[UBL Job #{job_id}] {msg_str}", flush=True)
        try:
            job.logs = logs
            job.save(update_fields=["logs"])
        except Exception:
            pass

    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(id=user_id).first()

        commit      = bool(data.get("commit",       True))
        no_proxy    = bool(data.get("no_proxy",     True))
        all_month   = bool(data.get("all_month",    False))
        days        = data.get("days",        None)
        start_date_raw = data.get("start_date", None)
        end_date_raw   = data.get("end_date",   None)
        reverify_all   = bool(data.get("reverify_all", False))
        use_local      = bool(data.get("local",        False))

        # ── Import services (deferred to avoid circular imports at startup) ──
        from myapp.Services.ubl_reconciliation import (
            reconcile_ubl_transfers,
            get_reconciliation_window,
            get_unverified_transfers_window,
            get_latest_statement_file,
        )
        from myapp.Services.UBL_scrapper import scrape_ubl_statement
        from myapp.Services.ubl_statement_sync import ingest_ubl_statement_file

        # ── 1. Resolve date window ──────────────────────────────────────────
        def _parse(d_str):
            if not d_str:
                return None
            d_str = str(d_str).strip()
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    return datetime.strptime(d_str, fmt).date()
                except ValueError:
                    continue
            return None

        start_date = _parse(start_date_raw)
        end_date   = _parse(end_date_raw)

        if all_month and not start_date:
            today = timezone.localtime(timezone.now()).date()
            start_date = date(today.year, today.month, 1)
            end_date   = today

        if not start_date or not end_date:
            if days:
                try:
                    start_date, end_date = get_reconciliation_window(
                        window_days=int(days), lag_days=1
                    )
                except Exception as e:
                    log(f"[WARN] get_reconciliation_window failed: {e}")
            if not start_date or not end_date:
                try:
                    start_date, end_date, _ = get_unverified_transfers_window(
                        lag_days=1, current_month_only=not all_month
                    )
                except Exception as e:
                    log(f"[WARN] get_unverified_transfers_window failed: {e}")

        mode_str = "commit" if commit else "dry_run"
        log(f"[START] UBL Auto-Verify | mode={mode_str} | range={start_date} → {end_date}")

        # ── 2. Scrape or use local statement ─────────────────────────────────
        statement_file       = None
        downloaded_temp_path = None
        scraper_result       = {"status": "skipped", "file": None, "error": None}

        if use_local:
            try:
                statement_file = get_latest_statement_file()
                log(f"[SCRAPER] Using latest local statement: {statement_file}")
                scraper_result = {"status": "ok", "file": str(statement_file), "error": None}
            except Exception as e:
                scraper_result = {"status": "error", "file": None, "error": str(e)}
                log(f"[SCRAPER ERROR] {e}")
                job.status = "failed"
                job.error_message = f"Local statement file error: {e}"
                job.completed_at = timezone.now()
                job.logs = logs
                job.save()
                return
        else:
            log("[SCRAPER] Launching UBL portal scraper browser automation…")
            try:
                from_date_str = start_date.strftime("%d/%m/%Y") if start_date else None
                to_date_str   = end_date.strftime("%d/%m/%Y")   if end_date   else None
                proxy_arg = False if no_proxy else None
                downloaded_temp_path = scrape_ubl_statement(
                    from_date=from_date_str,
                    to_date=to_date_str,
                    log_callback=log,
                    proxy=proxy_arg,
                )
                statement_file = str(downloaded_temp_path)
                log(f"[SCRAPER] Statement downloaded successfully: {downloaded_temp_path.name}")
                scraper_result = {
                    "status": "ok",
                    "file": downloaded_temp_path.name,
                    "error": None,
                }
            except Exception as e:
                log(f"[SCRAPER ERROR] {e}")
                scraper_result = {"status": "error", "file": None, "error": str(e)}
                job.status = "failed"
                job.error_message = f"Scraper error: {e}"
                job.completed_at = timezone.now()
                job.result = {
                    "mode": mode_str,
                    "date_range": {
                        "start": str(start_date) if start_date else None,
                        "end":   str(end_date)   if end_date   else None,
                    },
                    "scraper": scraper_result,
                    "statement_sync": None,
                    "reconciliation": None,
                    "logs": logs,
                }
                job.logs = logs
                job.save()
                return

        # ── 3. Ingest statement into BankStatementRecord ─────────────────────
        statement_sync_result = None
        if statement_file and os.path.exists(str(statement_file)):
            log("[SYNC] Ingesting statement transactions into BankStatementRecord…")
            try:
                ingest_res = ingest_ubl_statement_file(str(statement_file), sync_job=job)
                statement_sync_result = ingest_res
                log(
                    f"[SYNC] Statement ingested: {ingest_res.get('newly_inserted', 0)} new records "
                    f"(+{ingest_res.get('credits_inserted', 0)} credits, "
                    f"+{ingest_res.get('debits_inserted', 0)} debits, "
                    f"{ingest_res.get('skipped_duplicates', 0)} duplicates skipped)"
                )
            except Exception as ie:
                log(f"[SYNC WARN] Statement ingestion warning: {ie}")

        # ── 4. Run reconciliation ────────────────────────────────────────────
        log("[RECONCILE] Reconciling outgoing PKR transfers against statement records…")
        try:
            rec = reconcile_ubl_transfers(
                file_path=statement_file,
                start_date=start_date,
                end_date=end_date,
                dry_run=not commit,
                undo=False,
                source="manual_admin",
                only_unverified=not reverify_all,
            )
        except Exception as e:
            log(f"[RECONCILE ERROR] {e}")
            if downloaded_temp_path and os.path.exists(str(downloaded_temp_path)):
                try:
                    os.remove(str(downloaded_temp_path))
                except Exception:
                    pass
            job.status = "failed"
            job.error_message = f"Reconciliation error: {e}"
            job.completed_at = timezone.now()
            job.result = {
                "mode": mode_str,
                "date_range": {
                    "start": str(start_date) if start_date else None,
                    "end":   str(end_date)   if end_date   else None,
                },
                "scraper": scraper_result,
                "statement_sync": statement_sync_result,
                "reconciliation": None,
                "logs": logs,
            }
            job.logs = logs
            job.save()
            return

        log(
            f"[RECONCILE] Done — "
            f"{rec.get('golden_matches_count', 0)} verified, "
            f"{rec.get('discrepancies_count', 0)} flagged, "
            f"{rec.get('unmatched_count', 0)} unmatched"
        )
        if commit:
            log("[RECONCILE] Changes committed to database.")
        else:
            log("[RECONCILE] Dry-run complete — no database changes made.")

        # ── 5. Cleanup temp file ─────────────────────────────────────────────
        if downloaded_temp_path and os.path.exists(str(downloaded_temp_path)):
            try:
                os.remove(str(downloaded_temp_path))
                log("[CLEANUP] Deleted temporary statement file.")
            except Exception as e:
                log(f"[CLEANUP WARN] Could not delete temp file: {e}")

        # ── 6. Build reconciliation payload ──────────────────────────────────
        def _serialize_transfer(t):
            cust_name = ""
            if t.customer_bank_account and t.customer_bank_account.customer:
                c = t.customer_bank_account.customer
                cust_name = getattr(c, "full_name", "") or getattr(c, "email", "")
            holder = t.customer_bank_account.holder_name if t.customer_bank_account else ""
            bank_name = (
                t.customer_bank_account.bank.name
                if (t.customer_bank_account and t.customer_bank_account.bank)
                else ""
            )
            if t.incoming_payment:
                linked = f"Payment: {t.incoming_payment.reference}"
            else:
                linked_payments = list(t.payments.all())
                if linked_payments:
                    refs = [p.reference for p in linked_payments]
                    linked = f"Bulk ({len(refs)}): {', '.join(refs[:3])}" + (
                        "..." if len(refs) > 3 else ""
                    )
                else:
                    linked = "No payment linked"
            return {
                "reference":     t.reference,
                "amount_pkr":    str(t.amount_pkr),
                "transfer_date": str(t.transfer_date) if t.transfer_date else None,
                "customer_name": cust_name,
                "holder_name":   holder,
                "bank_name":     bank_name,
                "linked":        linked,
            }

        golden_matches = []
        for m in rec.get("golden_matches", []):
            t = m["transfer"]
            b = m["bank_record"]
            golden_matches.append({
                "transfer": _serialize_transfer(t),
                "bank_beneficiary": b.get("BENEFICIARY_NAME", ""),
                "bank_date": b.get("PARSED_DATE", ""),
            })

        discrepancies = []
        for m in rec.get("discrepancies", []):
            t = m["transfer"]
            b = m["bank_record"]
            discrepancies.append({
                "transfer": _serialize_transfer(t),
                "bank_beneficiary": b.get("BENEFICIARY_NAME", ""),
                "bank_date": b.get("PARSED_DATE", ""),
                "notes": m.get("notes", ""),
            })

        unmatched = []
        for m in rec.get("unmatched", []):
            t = m["transfer"]
            unmatched.append({"transfer": _serialize_transfer(t)})

        reconciliation_payload = {
            "total_transfers":      rec.get("total_transfers", 0),
            "total_in_window":      rec.get("total_in_window", rec.get("total_transfers", 0)),
            "skipped_verified":     rec.get("skipped_verified", 0),
            "skipped_flagged":      rec.get("skipped_flagged", 0),
            "golden_matches_count": rec.get("golden_matches_count", 0),
            "discrepancies_count":  rec.get("discrepancies_count", 0),
            "unmatched_count":      rec.get("unmatched_count", 0),
            "total_bank_debits":    rec.get("total_bank_debits", 0),
            "golden_matches":       golden_matches,
            "discrepancies":        discrepancies,
            "unmatched":            unmatched,
        }

        # ── 7. AuditLog entry ────────────────────────────────────────────────
        if user:
            AuditLog.record(
                user=user,
                action=AuditLog.ACTION_UPDATE,
                target_label="UBL Auto-Verify",
                description=(
                    f"Manual UBL verify completed by {user.email} | "
                    f"mode={mode_str} | range={start_date}→{end_date} | "
                    f"verified={rec.get('golden_matches_count', 0)} "
                    f"flagged={rec.get('discrepancies_count', 0)} "
                    f"unmatched={rec.get('unmatched_count', 0)}"
                ),
                metadata={
                    "mode": mode_str,
                    "job_id": job.id,
                    "start_date": str(start_date) if start_date else None,
                    "end_date":   str(end_date)   if end_date   else None,
                    "golden_matches_count": rec.get("golden_matches_count", 0),
                    "discrepancies_count":  rec.get("discrepancies_count", 0),
                    "unmatched_count":      rec.get("unmatched_count", 0),
                },
                ip=ip, ua=ua,
            )

        # ── 8. Complete job ──────────────────────────────────────────────────
        job.status = "completed"
        job.completed_at = timezone.now()
        job.logs = logs
        job.result = {
            "mode": mode_str,
            "date_range": {
                "start": str(start_date) if start_date else None,
                "end":   str(end_date)   if end_date   else None,
            },
            "scraper":        scraper_result,
            "statement_sync": statement_sync_result,
            "reconciliation": reconciliation_payload,
            "sync_job":       BankSyncJobSerializer(job).data,
            "logs":           logs,
        }
        job.save()

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[UBL Job #{job_id} FATAL ERROR] {exc}\n{tb}", flush=True)
        job.status = "failed"
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        logs.append(f"[FATAL ERROR] {exc}")
        job.logs = logs
        job.save()
    finally:
        close_old_connections()


class UblVerifyView(APIView):
    """
    Manually trigger the UBL payment auto-verification pipeline (Admin Only).

    Runs asynchronously in a background thread to prevent HTTP timeouts.
    Returns 202 Accepted immediately with the created `job_id`.
    Clients poll GET /api/v1/bank-audit/ubl-verify/<job_id>/ for live progress & results.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    parser_classes = [JSONParser]

    def post(self, request):
        data = request.data or {}
        ip, ua = _client_meta(request)
        commit    = bool(data.get("commit",    True))
        no_proxy  = bool(data.get("no_proxy",  True))
        use_local = bool(data.get("local",     False))

        # Create persistent job record
        job = BankSyncJob.objects.create(
            source="manual_admin",
            status="running",
            options=data,
            logs=[
                f"[QUEUED] Verification job triggered by {request.user.email} "
                f"(commit={commit}, no_proxy={no_proxy}, local={use_local}) at {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"
            ],
        )

        # Launch background thread
        thread = threading.Thread(
            target=_run_ubl_verify_background,
            args=(job.id, data, request.user.id, ip, ua),
            daemon=True,
        )
        thread.start()

        return Response(
            {
                "job_id": job.id,
                "status": "running",
                "message": "UBL verification pipeline started in background.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class UblVerifyStatusView(APIView):
    """Poll status, live logs, and results of a UBL auto-verify job (Admin Only)."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request, pk=None):
        if pk == "latest" or pk is None:
            job = BankSyncJob.objects.filter(source="manual_admin").first()
            if not job:
                return Response(
                    {"detail": "No UBL verification job found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            job = get_object_or_404(BankSyncJob, pk=pk)

        ser = BankSyncJobSerializer(job)
        return Response({
            "job": ser.data,
            "job_id": job.id,
            "status": job.status,
            "logs": job.logs or [],
            "result": job.result or None,
            "error_message": job.error_message,
        })
