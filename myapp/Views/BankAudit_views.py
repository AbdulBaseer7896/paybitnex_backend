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
"""
from datetime import datetime
from decimal import Decimal

from django.db.models import Q, Sum, Count
from django.http import FileResponse, Http404
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
