"""
Transaction workflow views. This is the heart of PayBitnex.

Customer:
    POST /transactions/payments/        → submit new payment
    GET  /transactions/payments/        → list own payments
    GET  /transactions/payments/{id}/   → detail

Accountant / Admin:
    GET  /transactions/payments/                → see all (with filters)
    POST /transactions/payments/{id}/verify/    → Stage 1: confirm proofs are genuine
    POST /transactions/payments/{id}/apply/     → Stage 2: set rate + fee → verify
    POST /transactions/payments/{id}/status/    → set reject / on_hold / manual
    POST /transactions/transfers/               → record outgoing PKR transfer
        (marks incoming payment as PKR_SENT + COMPLETED + distributes fees)
"""
from decimal import Decimal
from django.db import transaction as dbtx
from django.utils import timezone
from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Auth_models import UserRole
from myapp.Models.Audit_models import AuditLog
from myapp.Models.Transaction_models import (
    IncomingPayment, OutgoingPKRTransfer, TransactionStatus,
    TransactionStatusHistory,
)
from myapp.serializers.Transaction_serializers import (
    IncomingPaymentSerializer, IncomingPaymentCreateSerializer,
    AccountantApplySerializer, OutgoingTransferCreateSerializer,
    OutgoingTransferSerializer, StatusUpdateSerializer,
    PaymentVerifySerializer,
)
from myapp.Utils.permissions import IsAdminOrAccountant
from myapp.Utils.references import next_reference
from myapp.Utils.partner_ledger import distribute_fee_for_payment


# ---------- helpers ----------

def _record_status_change(payment, from_status, to_status, user, note=""):
    TransactionStatusHistory.objects.create(
        payment=payment,
        from_status=from_status,
        to_status=to_status,
        changed_by=user,
        note=note,
    )
    AuditLog.record(
        user=user, action=AuditLog.ACTION_STATE_CHANGE, target=payment,
        description=f"{payment.reference}: {from_status} → {to_status}",
        before={"status": from_status}, after={"status": to_status},
    )


# ---------- viewsets ----------

class IncomingPaymentViewSet(viewsets.ModelViewSet):
    """
    Mixed-role viewset.
    Customers see their own; staff see everything (with filters).
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    queryset = (
        IncomingPayment.objects
        .select_related(
            "customer", "currency", "merchant_account__bank",
            "handled_by", "verified_by",
        )
        .prefetch_related("status_history__changed_by")
        .all()
    )
    filterset_fields = ["status", "currency", "customer"]
    search_fields = [
        "reference", "external_transaction_id",
        "sender_name", "sender_company", "customer__email",
    ]
    ordering_fields = ["created_at", "amount", "status"]

    def get_serializer_class(self):
        if self.action == "create":
            return IncomingPaymentCreateSerializer
        return IncomingPaymentSerializer

    def get_queryset(self):
        u = self.request.user
        if u.role == UserRole.CUSTOMER:
            return self.queryset.filter(customer=u)
        return self.queryset

    # ---- customer submit ----
    def create(self, request, *args, **kwargs):
        if request.user.role != UserRole.CUSTOMER:
            return Response(
                {"detail": "Only customers can submit payments."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Gate: require KYC-approved profile before any payment can be submitted.
        from myapp.Models.Profile_models import CustomerProfile
        profile = CustomerProfile.objects.filter(user=request.user).first()
        if not profile or profile.kyc_status != CustomerProfile.KYC_APPROVED:
            return Response(
                {
                    "detail": (
                        "Your profile must be KYC-approved before you can submit "
                        "payments. Please complete your profile and wait for admin "
                        "approval."
                    ),
                    "kyc_status": profile.kyc_status if profile else "none",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        s = IncomingPaymentCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        with dbtx.atomic():
            ref = next_reference(IncomingPayment, prefix="PBX")
            payment = IncomingPayment.objects.create(
                customer=request.user,
                reference=ref,
                status=TransactionStatus.SUBMITTED,
                **s.validated_data,
            )
            _record_status_change(
                payment, from_status="", to_status=TransactionStatus.SUBMITTED,
                user=request.user, note="Submitted by customer",
            )
        return Response(
            IncomingPaymentSerializer(payment).data,
            status=status.HTTP_201_CREATED,
        )

    # ---- accountant: verify documents (STAGE 1) ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="verify",
    )
    def verify_documents(self, request, pk=None):
        """
        Stage 1 of review. The accountant confirms the uploaded proofs look
        legitimate, writes a short note, and optionally attaches their own
        reference document. Unlocks the Apply-Rate-Fee stage.

        - Forbidden once the payment is past VERIFIED.
        - Idempotent: calling again before rate/fee just updates note/doc.
        """
        payment = self.get_object()

        if payment.status in (TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED):
            return Response(
                {"detail": "Cannot modify a completed payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if payment.status == TransactionStatus.REJECTED:
            return Response(
                {"detail": "Payment is rejected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = PaymentVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        note = s.validated_data.get("note", "") or ""
        document = s.validated_data.get("document")

        was_already_verified = payment.is_verified
        before_status = payment.status

        with dbtx.atomic():
            payment.verified_note = note
            if document is not None:
                payment.verified_document = document
            payment.verified_by = request.user
            payment.verified_at = timezone.now()
            payment.handled_by = request.user

            # Move out of SUBMITTED → UNDER_REVIEW (don't overwrite VERIFIED
            # or later states)
            if payment.status == TransactionStatus.SUBMITTED:
                payment.status = TransactionStatus.UNDER_REVIEW

            payment.save()

            if before_status != payment.status:
                _record_status_change(
                    payment, before_status, payment.status,
                    user=request.user,
                    note="Documents reviewed" + (f" — {note}" if note else ""),
                )

            AuditLog.record(
                user=request.user,
                action=AuditLog.ACTION_UPDATE,
                target=payment,
                description=(
                    f"{payment.reference}: documents "
                    f"{'re-verified' if was_already_verified else 'verified'}"
                ),
                before=None,
                after={"verified_at": payment.verified_at.isoformat(), "note": note},
            )

        return Response(IncomingPaymentSerializer(payment).data)

    # ---- accountant: apply rate + fee (STAGE 2) ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="apply",
    )
    def apply_rate_and_fee(self, request, pk=None):
        payment = self.get_object()
        s = AccountantApplySerializer(data=request.data)
        s.is_valid(raise_exception=True)

        if payment.status in (TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED):
            return Response(
                {"detail": "Cannot modify a completed payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Must verify documents first
        if not payment.is_verified:
            return Response(
                {"detail":
                    "Documents must be verified before applying rate & fee."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        before_status = payment.status
        with dbtx.atomic():
            payment.exchange_rate = s.validated_data["exchange_rate"]
            payment.fee_percentage = s.validated_data["fee_percentage"]
            if "accountant_notes" in s.validated_data:
                payment.accountant_notes = s.validated_data["accountant_notes"]
            payment.calculate_amounts()
            payment.handled_by = request.user

            if s.validated_data.get("mark_verified") and payment.status != TransactionStatus.VERIFIED:
                payment.status = TransactionStatus.VERIFIED
            elif payment.status == TransactionStatus.SUBMITTED:
                # Should not happen — verify-first guard above — but safe default.
                payment.status = TransactionStatus.UNDER_REVIEW

            payment.save()

            if before_status != payment.status:
                _record_status_change(
                    payment, before_status, payment.status,
                    user=request.user, note="Rate + fee applied",
                )

        return Response(IncomingPaymentSerializer(payment).data)

    # ---- accountant: set status (reject / hold / manual) ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="status",
    )
    def set_status(self, request, pk=None):
        payment = self.get_object()
        s = StatusUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        new_status = s.validated_data["status"]
        if new_status not in TransactionStatus.values:
            raise ValidationError({"status": "Invalid status."})
        before = payment.status
        payment.status = new_status
        payment.handled_by = request.user
        payment.save(update_fields=["status", "handled_by", "updated_at"])
        _record_status_change(
            payment, before, new_status,
            user=request.user, note=s.validated_data.get("note", ""),
        )
        return Response(IncomingPaymentSerializer(payment).data)


class OutgoingTransferViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    """
    Accountant records the PKR transfer. On create:
    - marks the IncomingPayment as PKR_SENT then COMPLETED,
    - runs partner fee distribution,
    - logs status history + audit.
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = (
        OutgoingPKRTransfer.objects
        .select_related("incoming_payment", "customer_bank_account__bank", "sent_by")
        .all()
    )
    serializer_class = OutgoingTransferSerializer
    filterset_fields = ["sent_by", "customer_bank_account"]
    search_fields = ["reference", "bank_transaction_id"]

    def get_serializer_class(self):
        if self.action == "create":
            return OutgoingTransferCreateSerializer
        return OutgoingTransferSerializer

    def create(self, request, *args, **kwargs):
        s = OutgoingTransferCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        payment: IncomingPayment = s.validated_data["incoming_payment"]

        if payment.status in (TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED):
            raise ValidationError({"incoming_payment": "Already settled."})
        if payment.net_pkr is None:
            raise ValidationError({
                "incoming_payment":
                "Payment has no rate/fee applied. Apply rate first.",
            })

        with dbtx.atomic():
            before_status = payment.status
            ref = next_reference(OutgoingPKRTransfer, prefix="OUT")
            transfer = OutgoingPKRTransfer.objects.create(
                reference=ref,
                sent_by=request.user,
                **s.validated_data,
            )
            payment.status = TransactionStatus.PKR_SENT
            payment.handled_by = request.user
            payment.completed_at = timezone.now()
            payment.save(update_fields=[
                "status", "handled_by", "completed_at", "updated_at",
            ])
            _record_status_change(
                payment, before_status, TransactionStatus.PKR_SENT,
                user=request.user, note=f"PKR sent — ref {transfer.reference}",
            )

            # Distribute fees across partners
            distribute_fee_for_payment(payment)

            # Mark completed
            payment.status = TransactionStatus.COMPLETED
            payment.save(update_fields=["status", "updated_at"])
            _record_status_change(
                payment, TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED,
                user=request.user, note="Auto-completed after transfer + distribution",
            )

        return Response(
            OutgoingTransferSerializer(transfer).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------
# CUSTOMERS WITH TRANSACTION COUNTS
#   GET /transactions/customers-summary/
#
# Used by the "User Transactions" page (admin + accountant). Returns one row
# per customer who has at least one profile, with counts for each status
# bucket. Optional ?q= filters by email/full_name/cnic. Always includes only
# role=customer.
# ---------------------------------------------------------------------
from rest_framework.decorators import api_view, permission_classes as perm_classes
from myapp.Utils.permissions import IsAdminOrAccountant


@api_view(["GET"])
@perm_classes([IsAuthenticated, IsAdminOrAccountant])
def customers_with_tx_counts(request):
    from django.db.models import Count, Sum, Q, Max
    from myapp.Models.Auth_models import User

    # NOTE: The reverse relation from IncomingPayment.customer is
    # "incoming_payments" (defined via related_name), NOT the default
    # "incomingpayment". Using the wrong name throws FieldError at query time.
    qs = (User.objects
          .filter(role=UserRole.CUSTOMER)
          .select_related("profile")
          .annotate(
              total_tx=Count("incoming_payments"),
              pending_tx=Count(
                  "incoming_payments",
                  filter=Q(incoming_payments__status__in=[
                      TransactionStatus.SUBMITTED,
                      TransactionStatus.UNDER_REVIEW,
                  ]),
              ),
              verified_tx=Count(
                  "incoming_payments",
                  filter=Q(incoming_payments__status=TransactionStatus.VERIFIED),
              ),
              completed_tx=Count(
                  "incoming_payments",
                  filter=Q(incoming_payments__status=TransactionStatus.COMPLETED),
              ),
              rejected_tx=Count(
                  "incoming_payments",
                  filter=Q(incoming_payments__status=TransactionStatus.REJECTED),
              ),
              last_tx_at=Max("incoming_payments__created_at"),
              total_pkr=Sum(
                  "incoming_payments__net_pkr",
                  filter=Q(incoming_payments__status=TransactionStatus.COMPLETED),
              ),
          )
          .order_by("-last_tx_at", "-created_at"))

    q = request.query_params.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(email__icontains=q) |
            Q(full_name__icontains=q) |
            Q(profile__cnic_number__icontains=q)
        )

    kyc = request.query_params.get("kyc", "").strip()
    if kyc:
        qs = qs.filter(profile__kyc_status=kyc)

    only_active = request.query_params.get("only_active")
    if only_active == "true":
        qs = qs.filter(total_tx__gt=0)

    def _safe_file_url(file_field):
        """ImageField/FileField .url raises ValueError when the file is
        empty. Check .name first — empty strings mean 'no file'."""
        try:
            if file_field and getattr(file_field, "name", ""):
                return file_field.url
        except (ValueError, AttributeError):
            pass
        return None

    rows = []
    for u in qs[:500]:
        prof = None
        try:
            prof = u.profile
        except Exception:
            prof = None

        picture_url = (
            _safe_file_url(getattr(prof, "selfie", None)) if prof else None
        ) or _safe_file_url(getattr(u, "profile_picture", None))

        rows.append({
            "id": str(u.id),
            "email": u.email,
            "full_name": u.full_name or "",
            "is_active": u.is_active,
            "profile_picture_url": picture_url,
            "kyc_status": getattr(prof, "kyc_status", None),
            "cnic_number": getattr(prof, "cnic_number", None),
            "total_tx": u.total_tx or 0,
            "pending_tx": u.pending_tx or 0,
            "verified_tx": u.verified_tx or 0,
            "completed_tx": u.completed_tx or 0,
            "rejected_tx": u.rejected_tx or 0,
            "last_tx_at": u.last_tx_at.isoformat() if u.last_tx_at else None,
            "total_pkr_received": str(u.total_pkr or 0),
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return Response({"count": len(rows), "results": rows})
