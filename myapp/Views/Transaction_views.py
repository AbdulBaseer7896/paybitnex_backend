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
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.references import next_reference
from myapp.Utils.partner_ledger import distribute_fee_for_payment


from myapp.Utils.email_tasks import send_email_async


# ---------- helpers ----------

# Human-readable labels for email display — keys must match TransactionStatus
STATUS_DISPLAY = {
    "submitted":    "Submitted — under initial review",
    "under_review": "Under review by our team",
    "verified":     "Verified — awaiting rate & fee application",
    "pkr_sent":     "PKR sent to your account",
    "completed":    "Completed",
    "on_hold":      "On hold — additional review needed",
    "rejected":     "Rejected",
}

# Brief "what's next" copy for customer reassurance in the email.
STATUS_NEXT_STEP = {
    "under_review": "Our team is reviewing the payment details. No action needed from you.",
    "verified":     "Your payment has been verified. We're applying the final rate and fee.",
    "pkr_sent":     "Funds have been transferred to your PKR account. Please allow a short time for them to appear.",
    "completed":    "This payment is now complete. Thanks for using PayBitnex.",
    "on_hold":      "We need to recheck some details — we'll follow up if we need anything from you.",
    "rejected":     "Unfortunately we were unable to process this payment. Please contact support for details.",
}


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

    # Customer-facing email — ONLY sent when a staff member changed the
    # status, not when the payment was first submitted (from_status="").
    # The email is addressed to the customer alone; no admin/accountant
    # addresses are in `to` or `cc`, so their emails are never exposed
    # to customers.
    if from_status and to_status != from_status:
        try:
            customer = payment.customer
            if customer and customer.email:
                send_email_async(
                    to=[customer.email],
                    subject=f"Payment {payment.reference}: status updated",
                    template="payments/status_update",
                    context={
                        "name": customer.full_name or "",
                        "reference": payment.reference,
                        "amount": f"{payment.amount}",
                        "currency": payment.currency_id,
                        "status_label": STATUS_DISPLAY.get(
                            to_status, to_status.replace("_", " ").title(),
                        ),
                        "next_step": STATUS_NEXT_STEP.get(to_status, ""),
                    },
                )
        except Exception:
            # Never let email failure block the status-change response.
            pass


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
            "customer", "currency",
            "payment_method",
            "handled_by", "verified_by",
            "outgoing_transfer",
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
        qs = self.queryset
        if u.role == UserRole.CUSTOMER:
            # Customers see every one of their own payments, including stale
            # ones — they're the ones who need to confirm those.
            qs = qs.filter(customer=u)
            return self._apply_date_filter(qs)

        # Staff: apply the stale filter ONLY on the list action. For a
        # retrieve (detail page), custom @action endpoints, or update,
        # we must return the full unfiltered queryset — otherwise staff
        # can't open, edit, or force-complete a payment whose URL they
        # navigate to directly, because it's been filtered out as stale.
        if self.action != "list":
            return qs

        # List view: by default exclude stale PKR_SENT payments so the
        # main transactions list stays focused on active work. The
        # "Awaiting customer confirmation" page passes `?only_stale=true`
        # to flip the filter, or `?include_stale=true` to see both mixed.
        #
        # Staleness is computed on the fly by comparing `updated_at`
        # against the configured threshold minutes, rather than relying
        # solely on the `is_stale` DB flag. This means the Awaiting page
        # reflects reality instantly even without celery-beat running.
        from datetime import timedelta
        from django.db.models import Q

        p = self.request.query_params
        only_stale = p.get("only_stale") in ("1", "true", "True", "yes")
        include_stale = p.get("include_stale") in ("1", "true", "True", "yes")

        from myapp.Utils.stale_payment_tasks import _resolve_threshold_minutes
        minutes = _resolve_threshold_minutes()
        cutoff = timezone.now() - timedelta(minutes=minutes)

        stale_q = (
            Q(status=TransactionStatus.PKR_SENT)
            & (Q(is_stale=True) | Q(updated_at__lt=cutoff))
        )

        if only_stale:
            qs = qs.filter(stale_q)
        elif not include_stale:
            qs = qs.exclude(stale_q)

        return self._apply_date_filter(qs)

    def _apply_date_filter(self, qs):
        """Apply ?date_from / ?date_to query params if present.

        Both bounds are inclusive and operate on `created_at::date` so
        users get the calendar-day intuition they expect ("from Apr 1 to
        Apr 30" includes anything on Apr 1 from 00:00 onward and
        anything on Apr 30 up to 23:59:59). Invalid strings are ignored
        silently — DRF would 400 on a filter, but we don't want a
        malformed URL to break the page.
        """
        from datetime import datetime
        p = self.request.query_params
        df = p.get("date_from")
        dt = p.get("date_to")
        try:
            if df:
                df_date = datetime.strptime(df, "%Y-%m-%d").date()
                qs = qs.filter(created_at__date__gte=df_date)
            if dt:
                dt_date = datetime.strptime(dt, "%Y-%m-%d").date()
                qs = qs.filter(created_at__date__lte=dt_date)
        except (ValueError, TypeError):
            # Bad date string → just skip filtering rather than raising.
            pass
        return qs

    # ---- admin / accountant: CSV export of (filtered) transactions ----
    @action(
        detail=False, methods=["get"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="export.csv",
    )
    def export_csv(self, request):
        """
        GET /transactions/payments/export.csv

        Streams a CSV of incoming payments matching the same filters
        the list endpoint accepts: ?customer=, ?status=, ?date_from=,
        ?date_to=, ?search=. Designed to back the "Export CSV" button
        on the per-customer transactions view.

        The Content-Disposition filename includes the customer's name
        (slugified) when ?customer= is present so downloads are easy
        to organize, otherwise it falls back to a date-stamped name.
        """
        import csv
        import io
        from datetime import datetime
        from django.http import StreamingHttpResponse

        # Re-use the filterset / search-fields machinery from the
        # ListModelMixin so the export honours the exact same filters
        # as the list endpoint. We deliberately bypass `get_queryset`'s
        # stale-exclusion when the caller is exporting — they may want
        # to see all completed transactions including stale ones — but
        # we honour an explicit ?only_stale flag.
        qs = (
            IncomingPayment.objects
            .select_related(
                "customer", "currency", "payment_method",
                "verified_by", "handled_by",
            )
        )
        # Customer scope filter
        cust_id = request.query_params.get("customer")
        if cust_id:
            qs = qs.filter(customer_id=cust_id)
        # Status filter
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        # Currency filter (rare, but the list view supports it)
        cur = request.query_params.get("currency")
        if cur:
            qs = qs.filter(currency_id=cur)
        # Search across reference / external id / sender / customer email
        search = request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(reference__icontains=search)
                | Q(external_transaction_id__icontains=search)
                | Q(sender_name__icontains=search)
                | Q(sender_company__icontains=search)
                | Q(customer__email__icontains=search)
            )
        # Date range — inclusive of both bounds, calendar-day semantics
        try:
            df = request.query_params.get("date_from")
            dt_ = request.query_params.get("date_to")
            if df:
                qs = qs.filter(
                    created_at__date__gte=datetime.strptime(df, "%Y-%m-%d").date(),
                )
            if dt_:
                qs = qs.filter(
                    created_at__date__lte=datetime.strptime(dt_, "%Y-%m-%d").date(),
                )
        except (ValueError, TypeError):
            pass

        qs = qs.order_by("-created_at")

        # Build the CSV in memory. For the data sets this app handles
        # (low thousands per customer), in-memory is fine. If we ever
        # need to handle millions of rows we'd swap to the streaming
        # `csv.writer` + generator pattern.
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Reference",
            "Submitted at",
            "Customer name",
            "Customer email",
            "Sender name",
            "Sender company",
            "Sender bank",
            "External tx ID",
            "Currency",
            "Amount",
            "Exchange rate",
            "Fee %",
            "Fee (foreign)",
            "Net (foreign)",
            "Gross PKR",
            "Net PKR",
            "Status",
            "Verified by",
            "Verified at",
            "Completed at",
            "Payment method",
        ])

        def fmt_dt(value):
            return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""

        def fmt_d(value):
            return value.strftime("%Y-%m-%d") if value else ""

        def s(value):
            return "" if value is None else str(value)

        for tx in qs.iterator(chunk_size=500):
            cust = tx.customer
            pm = tx.payment_method
            writer.writerow([
                tx.reference,
                fmt_dt(tx.created_at),
                cust.full_name or "",
                cust.email or "",
                tx.sender_name or "",
                tx.sender_company or "",
                tx.sender_bank_name or "",
                tx.external_transaction_id or "",
                tx.currency_id or "",
                s(tx.amount),
                s(tx.exchange_rate),
                s(tx.fee_percentage),
                s(tx.fee_amount_foreign),
                s(tx.net_amount_foreign),
                s(tx.gross_pkr),
                s(tx.net_pkr),
                tx.status or "",
                (tx.verified_by.full_name or tx.verified_by.email) if tx.verified_by_id else "",
                fmt_dt(tx.verified_at),
                fmt_dt(tx.completed_at),
                pm.label if pm else "",
            ])

        # Build a sensible filename. When exporting a specific customer's
        # transactions we slugify their name so downloads are easy to
        # tell apart in a Downloads folder full of CSVs.
        slug_part = "all-customers"
        if cust_id:
            from myapp.Models.Auth_models import User
            target = User.objects.filter(pk=cust_id).first()
            if target is not None:
                raw = target.full_name or target.email or "customer"
                slug_part = "".join(
                    ch.lower() if ch.isalnum() else "-"
                    for ch in raw
                ).strip("-") or "customer"
        date_stamp = timezone.now().strftime("%Y-%m-%d")
        filename = f"transactions-{slug_part}-{date_stamp}.csv"

        body = buf.getvalue().encode("utf-8")
        # Add BOM so Excel on Windows opens UTF-8 correctly without
        # mangling non-ASCII characters in customer names. Cheap and
        # universally understood.
        body = b"\xef\xbb\xbf" + body

        resp = StreamingHttpResponse(
            iter([body]), content_type="text/csv; charset=utf-8",
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

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

        # --- Gate (item 10): prevent moving to any "advancing" state without
        # rate + fee applied. We allow REJECTED / ON_HOLD / UNDER_REVIEW /
        # SUBMITTED transitions even without rate+fee, because those aren't
        # "completion" outcomes. But advancing to VERIFIED / PKR_SENT /
        # COMPLETED requires rate+fee already in place. ---
        advancing_states = {
            TransactionStatus.VERIFIED,
            TransactionStatus.PKR_SENT,
            TransactionStatus.COMPLETED,
        }
        if new_status in advancing_states and not payment.is_rate_fee_applied:
            raise ValidationError({
                "status":
                "Apply exchange rate and fee before advancing the payment status.",
            })

        before = payment.status
        payment.status = new_status
        payment.handled_by = request.user
        payment.save(update_fields=["status", "handled_by", "updated_at"])
        _record_status_change(
            payment, before, new_status,
            user=request.user, note=s.validated_data.get("note", ""),
        )
        return Response(IncomingPaymentSerializer(payment).data)

    # ---- accountant / admin: unverify (item 7) ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="unverify",
    )
    def unverify_documents(self, request, pk=None):
        """
        Reverse a verification. Flips `verified_at` back to null and rolls the
        status back to UNDER_REVIEW (unless already past PKR_SENT, in which
        case we refuse). Used by the admin/accountant when they clicked
        "Verify" in error.
        """
        payment = self.get_object()
        if payment.verified_at is None:
            return Response(
                {"detail": "Payment is not currently verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if payment.status in (TransactionStatus.PKR_SENT, TransactionStatus.COMPLETED):
            return Response(
                {"detail":
                 "Cannot unverify after PKR has been sent. Reverse the "
                 "PKR transfer record first if this was an error."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before_status = payment.status
        with dbtx.atomic():
            payment.verified_at = None
            payment.verified_by = None
            payment.verified_note = ""
            payment.status = TransactionStatus.UNDER_REVIEW
            payment.handled_by = request.user
            payment.save(update_fields=[
                "verified_at", "verified_by", "verified_note",
                "status", "handled_by", "updated_at",
            ])
            if before_status != payment.status:
                _record_status_change(
                    payment, before_status, payment.status,
                    user=request.user, note="Verification reversed",
                )
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                target=payment,
                description=f"{payment.reference}: verification reversed",
            )
        return Response(IncomingPaymentSerializer(payment).data)

    # ---- customer: "I received my PKR" ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated],
        url_path="customer-confirm",
    )
    def customer_confirm(self, request, pk=None):
        """
        Customer clicks "I received my PKR" on their portal after the
        accountant has recorded an OutgoingPKRTransfer. Flips status to
        COMPLETED and fires the partner fee-distribution logic.
        """
        from myapp.Models.Auth_models import UserRole
        from myapp.Utils.partner_ledger import distribute_fee_for_payment
        from myapp.serializers.Transaction_serializers import CustomerConfirmSerializer

        payment = self.get_object()

        # Only the owning customer can confirm. Staff have their own path
        # (force_complete) — they should NOT be hitting this endpoint.
        if request.user.role != UserRole.CUSTOMER or payment.customer_id != request.user.id:
            return Response(
                {"detail": "Only the payment's customer can confirm receipt."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if payment.status != TransactionStatus.PKR_SENT:
            return Response(
                {"detail":
                 "Payment is not awaiting customer confirmation "
                 f"(current status: {payment.get_status_display()})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = CustomerConfirmSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        note = s.validated_data.get("note", "") or ""

        before_status = payment.status
        with dbtx.atomic():
            payment.customer_confirmed_at = timezone.now()
            payment.completed_at = timezone.now()
            payment.status = TransactionStatus.COMPLETED
            # Confirming un-stales a stale payment if this happened after the
            # daily task already flagged it.
            payment.is_stale = False
            payment.save(update_fields=[
                "customer_confirmed_at", "completed_at",
                "status", "is_stale", "updated_at",
            ])
            _record_status_change(
                payment, before_status, TransactionStatus.COMPLETED,
                user=request.user,
                note=("Customer confirmed receipt"
                      + (f" — {note}" if note else "")),
            )
            # Run partner fee distribution now (was previously run at
            # PKR_SENT; new flow runs it at true completion).
            distribute_fee_for_payment(payment)
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                target=payment,
                description=f"{payment.reference}: customer confirmed PKR receipt",
            )
        return Response(IncomingPaymentSerializer(payment).data)

    # ---- admin: force complete a stale payment ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdmin],
        url_path="force-complete",
    )
    def force_complete(self, request, pk=None):
        """
        Admin-only override for the "Awaiting customer confirmation" queue.
        Moves a stale PKR_SENT payment to COMPLETED on the customer's behalf,
        runs fee distribution, and logs a reason + `force_completed_by` on
        the payment for the audit trail.
        """
        from myapp.Utils.partner_ledger import distribute_fee_for_payment
        from myapp.serializers.Transaction_serializers import ForceCompleteSerializer

        payment = self.get_object()
        if payment.status != TransactionStatus.PKR_SENT:
            return Response(
                {"detail":
                 "Only PKR-sent payments awaiting customer confirmation "
                 "can be force-completed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        s = ForceCompleteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        reason = s.validated_data["reason"]

        before_status = payment.status
        with dbtx.atomic():
            payment.force_completed_by = request.user
            payment.force_completed_at = timezone.now()
            payment.completed_at = timezone.now()
            payment.status = TransactionStatus.COMPLETED
            payment.is_stale = False
            payment.save(update_fields=[
                "force_completed_by", "force_completed_at", "completed_at",
                "status", "is_stale", "updated_at",
            ])
            _record_status_change(
                payment, before_status, TransactionStatus.COMPLETED,
                user=request.user,
                note=f"Force-completed by admin — reason: {reason}",
            )
            distribute_fee_for_payment(payment)
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                target=payment,
                description=(
                    f"{payment.reference}: force-completed by admin. "
                    f"Reason: {reason}"
                ),
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

        # --- Gate (item 10): rate + fee MUST be applied before recording PKR
        # transfer. This is enforced server-side so any client that skips the
        # UI block still can't bypass it. ---
        if not payment.is_rate_fee_applied:
            raise ValidationError({
                "incoming_payment":
                "Apply exchange rate and fee before recording the PKR transfer.",
            })

        with dbtx.atomic():
            before_status = payment.status
            ref = next_reference(OutgoingPKRTransfer, prefix="OUT")
            transfer = OutgoingPKRTransfer.objects.create(
                reference=ref,
                sent_by=request.user,
                **s.validated_data,
            )
            # --- NEW flow: stop at PKR_SENT and wait for customer confirmation.
            # The customer portal will show a "I received my PKR" button; when
            # they click it, status flips to COMPLETED and partner fees are
            # distributed (see `customer_confirm` and `force_complete`
            # actions on IncomingPaymentViewSet).
            payment.status = TransactionStatus.PKR_SENT
            payment.handled_by = request.user
            payment.save(update_fields=["status", "handled_by", "updated_at"])
            _record_status_change(
                payment, before_status, TransactionStatus.PKR_SENT,
                user=request.user, note=f"PKR sent — ref {transfer.reference}",
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
    # Cap at 200 rows — this is a summary widget, not an export.
    # For bulk exports use the /reports/ endpoint with pagination.
    for u in qs[:200]:
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

    return Response({"count": len(rows), "results": rows, "capped": len(rows) >= 200})
