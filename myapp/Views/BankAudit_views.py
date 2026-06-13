"""
Bank-reconciliation audit views (admin only).

Endpoints (mounted under /api/v1/bank-audit/):

  POST   run/                 → run an ad-hoc reconciliation (no save).
                                multipart: bank, file, [start], [end].
                                Returns the full result JSON.

  GET    audits/              → list saved audits (history).
  POST   audits/              → save an audit: multipart bank, file,
                                title, [start], [end], [notes].
                                Re-runs the reconciliation server-side
                                (so the frozen result is trustworthy)
                                and stores the statement file.
  GET    audits/<id>/         → saved-audit detail (full result + files).
  DELETE audits/<id>/         → delete a saved audit (+ its file).
  GET    audits/<id>/download/ → download the stored statement file.
"""
from datetime import datetime

from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from myapp.Models.Audit_models import AuditLog
from myapp.Models.BankAudit_models import BankAudit, BankAuditFile
from myapp.serializers.BankAudit_serializers import (
    BankAuditListSerializer, BankAuditDetailSerializer,
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
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
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
        except Exception as e:  # pragma: no cover — surface parse crashes cleanly
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

        # Re-run the reconciliation server-side so the saved result is
        # authoritative (never trust a client-supplied result blob).
        try:
            result = run_audit(
                bank, upload, filename=getattr(upload, "name", ""),
                start=start, end=end,
            )
        except ValueError as e:
            return Response({"detail": str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:  # pragma: no cover
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

        # The file pointer was consumed by run_audit; reset before saving.
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
            description=f"Saved bank audit: {audit.title} "
                        f"({audit.get_bank_display()})",
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
        # Best-effort: delete the stored statement blobs too.
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
