"""BankAlertEmail views — the audit / reconciliation tabs."""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone

from myapp.Models.Audit_models import AuditLog
from myapp.Models.BankEmail_models import BankAlertEmail, BankAlertStatus
from myapp.Models.Transaction_models import (
    IncomingPayment, TransactionStatus, TransactionStatusHistory,
)
from myapp.serializers.BankEmail_serializers import (
    BankAlertEmailSerializer, BankAlertEmailListSerializer,
)
from myapp.Utils.permissions import IsAdminOrAccountant


class BankAlertEmailViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /transactions/bank-emails/                 — all (paginated)
    GET  /transactions/bank-emails/?status=unmatched
    GET  /transactions/bank-emails/{id}/            — detail (with raw body)
    POST /transactions/bank-emails/{id}/match/      — link to a payment
    POST /transactions/bank-emails/{id}/ignore/     — dismiss
    POST /transactions/bank-emails/trigger-fetch/   — run the poller now
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = (
        BankAlertEmail.objects
        .select_related("matched_payment", "matched_payment__customer", "matched_by")
        .all()
    )
    filterset_fields = ["status", "bank_type", "extracted_currency"]
    search_fields = [
        "subject", "sender_email", "extracted_sender_name", "extracted_reference",
    ]
    ordering_fields = ["received_at", "created_at", "extracted_amount"]
    ordering = ["-received_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return BankAlertEmailListSerializer
        return BankAlertEmailSerializer

    # ── Convenience filters ──────────────────────────────────────────
    @action(detail=False, methods=["get"])
    def unmatched(self, request):
        qs = self.filter_queryset(
            self.get_queryset().filter(status=BankAlertStatus.UNMATCHED)
        )
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(
            BankAlertEmailListSerializer(page, many=True).data
        )

    @action(detail=False, methods=["get"])
    def conflicts(self, request):
        qs = self.filter_queryset(
            self.get_queryset().filter(status=BankAlertStatus.CONFLICT)
        )
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(
            BankAlertEmailListSerializer(page, many=True).data
        )

    # ── Manual match ─────────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def match(self, request, pk=None):
        """Body: { "payment_id": "<uuid>", "notes": "optional" }"""
        email_rec = self.get_object()
        payment_id = request.data.get("payment_id")
        if not payment_id:
            return Response(
                {"payment_id": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payment = IncomingPayment.objects.select_related("customer").get(pk=payment_id)
        except IncomingPayment.DoesNotExist:
            return Response(
                {"payment_id": "No payment with this id."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if payment.status in (TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED):
            return Response(
                {"payment_id": "Payment is already settled — cannot re-match."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        before = payment.status
        payment.verified_at = payment.verified_at or timezone.now()
        payment.verified_by = request.user
        payment.verified_note = (
            f"Manually matched to bank email "
            f"({email_rec.bank_type or 'unknown'}) by {request.user.email}"
        )
        payment.auto_verified = True
        payment.linked_bank_email = email_rec
        if payment.status == TransactionStatus.SUBMITTED:
            payment.status = TransactionStatus.UNDER_REVIEW
        payment.save(update_fields=[
            "verified_at", "verified_by", "verified_note",
            "auto_verified", "linked_bank_email", "status", "updated_at",
        ])

        if before != payment.status:
            TransactionStatusHistory.objects.create(
                payment=payment, from_status=before, to_status=payment.status,
                changed_by=request.user,
                note=f"Manually matched to bank email {email_rec.id}",
            )

        email_rec.status = BankAlertStatus.MANUAL_MATCHED
        email_rec.matched_payment = payment
        email_rec.matched_by = request.user
        email_rec.matched_at = timezone.now()
        if request.data.get("notes"):
            email_rec.notes = (email_rec.notes + "\n" + request.data["notes"]).strip()
        email_rec.save(update_fields=[
            "status", "matched_payment", "matched_by", "matched_at",
            "notes", "updated_at",
        ])

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=payment,
            description=(
                f"{payment.reference}: manually matched to bank email "
                f"{email_rec.id} ({email_rec.bank_type})"
            ),
        )
        return Response(
            {"detail": "Matched.", "payment_id": str(payment.id)},
            status=status.HTTP_200_OK,
        )

    # ── Dismiss ──────────────────────────────────────────────────────
    @action(detail=True, methods=["post"])
    def ignore(self, request, pk=None):
        email_rec = self.get_object()
        email_rec.status = BankAlertStatus.IGNORED
        if request.data.get("notes"):
            email_rec.notes = (email_rec.notes + "\n" + request.data["notes"]).strip()
        email_rec.save(update_fields=["status", "notes", "updated_at"])
        return Response({"detail": "Ignored."})

    # ── Force a poll right now ───────────────────────────────────────
    @action(detail=False, methods=["post"], url_path="trigger-fetch")
    def trigger_fetch(self, request):
        from myapp.Utils.bank_email_tasks import poll_bank_emails
        try:
            result = poll_bank_emails.delay()
            return Response({"task_id": str(result.id), "state": "queued"})
        except Exception as exc:
            return Response(
                {"detail": f"Could not queue task: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )