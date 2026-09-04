"""
Transaction workflow views. This is the heart of PaidiX.

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
import logging
from decimal import Decimal
from django.conf import settings
from django.db import transaction as dbtx
from django.db.models import Prefetch
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
    IncomingPayment, OutgoingPKRTransfer, OutgoingPKRTransferReceipt, TransactionStatus,
    TransactionStatusHistory,
)
from myapp.serializers.Transaction_serializers import (
    IncomingPaymentSerializer, IncomingPaymentDashboardSerializer,
    IncomingPaymentCreateSerializer,
    AccountantApplySerializer, OutgoingTransferCreateSerializer,
    OutgoingTransferSerializer, StatusUpdateSerializer,
    PaymentVerifySerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.references import next_reference
from myapp.Utils.default_rate import apply_default_rate
from myapp.Utils.partner_ledger import distribute_fee_for_payment


from myapp.Utils.email_tasks import send_email_async
from myapp.Utils.staff_alerts import notify_staff

log = logging.getLogger(__name__)


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
    "completed":    "This payment is now complete. Thanks for using PaidiX.",
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
            # Never let email failure block the status-change response, but
            # don't swallow it silently either — a bad SMTP/Resend config
            # otherwise looks identical to "no email was supposed to go out".
            log.exception(
                "customer status email failed: payment=%s %s → %s",
                payment.reference, from_status, to_status,
            )


def _payment_alert_context(payment):
    """Shared body fields for the staff-facing payment alerts."""
    return {
        "reference":      payment.reference,
        "customer_name":  payment.customer.full_name or payment.customer.email,
        "customer_email": payment.customer.email,
        "amount":         f"{payment.amount}",
        "currency":       payment.currency_id,
        "payment_method": str(payment.payment_method or ""),
        "sender_name":    payment.sender_name or "",
        "sender_company": payment.sender_company or "",
        "occurred_on":    payment.occurred_on or "",
    }


def _notify_admins_new_payment(payment, submitted_by=""):
    """Tell staff a payment is sitting in the review queue.

    Submission only. Every later transition is the customer's email (see
    `_record_status_change`), which deliberately skips from_status="" so the
    customer isn't emailed about their own submission.
    """
    notify_staff(
        subject=(
            f"New payment {payment.reference} — "
            f"{payment.amount} {payment.currency_id} pending review"
        ),
        template="payments/admin_new_payment",
        context={**_payment_alert_context(payment), "submitted_by": submitted_by},
        path=f"/transactions/{payment.id}",
        # Replies land on the customer — who staff would chase for a missing
        # proof or a wrong amount.
        reply_to=[payment.customer.email] if payment.customer.email else None,
    )


def _notify_admins_payment_confirmed(payment, confirmed_by=""):
    """Tell staff the payment is fully completed and closed out.

    This is the end of the money's journey — partner fees are distributed on
    the same transition — so it's the point at which the books can be
    reconciled, not just another status bump.
    """
    notify_staff(
        subject=(
            f"Payment {payment.reference} completed"
        ),
        template="payments/admin_payment_confirmed",
        context={**_payment_alert_context(payment), "confirmed_by": confirmed_by},
        path=f"/transactions/{payment.id}",
        reply_to=[payment.customer.email] if payment.customer.email else None,
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
            "customer", "currency",
            "payment_method",
            "handled_by", "verified_by",
            "force_completed_by",
            "outgoing_transfer",
        )
        .prefetch_related(
            "status_history__changed_by",
            "outgoing_transfer__receipts",
            # Feeds IncomingPaymentSerializer._transfer. Pre-ordered by
            # -sent_at so the serializer can take element 0 in Python rather
            # than issuing its own ordered query per row — that per-row query,
            # multiplied by the seven transfer fields, is what made a 500-row
            # page take ~11s.
            Prefetch(
                "covering_transfers",
                queryset=OutgoingPKRTransfer.objects
                .select_related("sent_by")
                .prefetch_related("receipts")
                .order_by("-sent_at"),
            ),
        )
        .all()
    )
    filterset_fields = ["status", "currency", "customer"]
    search_fields = [
        "reference", "external_transaction_id",
        "sender_name", "sender_company",
        "customer__full_name", "customer__email",
    ]
    # `tx_date` is an annotation added in get_queryset — the *business* /
    # transaction date (occurred_on, falling back to the entry date for
    # legacy rows). Lists sort by it so the admin sees payments in true
    # transaction-date order, not just entry order.
    ordering_fields = ["created_at", "occurred_on", "tx_date", "amount", "status"]

    def _is_dashboard_list(self):
        """Recognize both the explicit and legacy admin-dashboard request.

        Older/cached frontend bundles request the 500-row, include-stale list
        without ``view=dashboard``. Keep that exact request on the compact
        path so backend performance does not depend on a frontend deployment.
        """
        if self.action != "list":
            return False
        p = self.request.query_params
        if p.get("view") == "dashboard":
            return True
        return (
            getattr(self.request.user, "role", None) != UserRole.CUSTOMER
            and p.get("page_size") == "500"
            and p.get("include_stale") in ("1", "true", "True", "yes")
            and p.get("ordering") == "-created_at"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return IncomingPaymentCreateSerializer
        if self._is_dashboard_list():
            return IncomingPaymentDashboardSerializer
        return IncomingPaymentSerializer

    def get_queryset(self):
        u = self.request.user
        # Annotate the *transaction* date: `occurred_on` (the business date
        # the customer / staff entered for the payment), falling back to the
        # entry date for legacy rows that predate the field. Filtering AND
        # ordering both use this so "the date" always means the transaction
        # date, with `created_at` kept purely as the submitted-at reference.
        from django.db.models.functions import Coalesce, TruncDate
        from django.db.models import DateField
        qs = self.queryset.annotate(
            tx_date=Coalesce(
                "occurred_on",
                TruncDate("created_at"),
                output_field=DateField(),
            ),
        )
        if self._is_dashboard_list():
            # The dashboard serializer is relation-free. Drop the expensive
            # history/transfer prefetches and fetch only the columns it reads.
            qs = qs.prefetch_related(None).select_related(None).only(
                "id", "customer_id", "currency_id", "amount",
                "exchange_rate", "real_exchange_rate", "fee_percentage",
                "fee_amount_foreign", "net_pkr", "is_rate_provisional",
                "status", "occurred_on", "created_at", "updated_at",
                "is_stale",
            )
        if u.role == UserRole.CUSTOMER:
            # Customers see every one of their own payments, including stale
            # ones — they're the ones who need to confirm those.
            qs = qs.filter(customer=u)
            return self._apply_date_filter(qs)

        # Staff: apply the stale filter ONLY on the list action. For a
        # retrieve (detail page) or update, we return the full unfiltered queryset
        # so staff can open, edit, or force-complete any payment directly.
        if self.action == "summary":
            return self._apply_date_filter(qs)

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

        `date_by` parameter selects which date field to filter against:
        - "tx_date" or "payment" (DEFAULT): filters by payment / transaction date (`tx_date` / `occurred_on`).
        - "created_at" or "submission": filters by submission date (`created_at__date`).
        """
        from datetime import datetime
        p = self.request.query_params
        df = p.get("date_from")
        dt = p.get("date_to")
        date_by = (p.get("date_by") or "tx_date").lower()
        field_name = "created_at__date" if date_by in ("submission", "created_at") else "tx_date"

        try:
            if df:
                df_date = datetime.strptime(df, "%Y-%m-%d").date()
                qs = qs.filter(**{f"{field_name}__gte": df_date})
            if dt:
                dt_date = datetime.strptime(dt, "%Y-%m-%d").date()
                qs = qs.filter(**{f"{field_name}__lte": dt_date})
        except (ValueError, TypeError):
            # Bad date string → just skip filtering rather than raising.
            pass
        return qs

    # ---- server-side summary for overview dashboard & customer portal ----
    @action(
        detail=False, methods=["get"],
        permission_classes=[IsAuthenticated],
        url_path="summary", 
    )
    def summary(self, request):
        """Server-side aggregated metrics computed entirely in SQL."""
        from django.db.models import Sum, Count, F, Q, DecimalField
        qs = self.filter_queryset(self.get_queryset()).order_by()
        is_customer = getattr(request.user, "role", None) == UserRole.CUSTOMER

        total_count = qs.count()
        non_rejected = qs.exclude(status=TransactionStatus.REJECTED)
        completed_qs = qs.filter(status=TransactionStatus.COMPLETED)

        submitted_total = non_rejected.aggregate(v=Sum("amount"))["v"] or Decimal("0")

        by_currency_rows = non_rejected.values("currency_id").annotate(
            total=Sum("amount"), count=Count("id")
        )
        submitted_by_currency = {
            r["currency_id"]: {"total": float(r["total"] or 0), "count": r["count"]}
            for r in by_currency_rows
        }

        gross_pkr_received = completed_qs.aggregate(
            v=Sum(F("amount") * F("exchange_rate"), output_field=DecimalField(max_digits=20, decimal_places=2))
        )["v"] or Decimal("0")

        transferred = completed_qs.aggregate(v=Sum("net_pkr"))["v"] or Decimal("0")

        status_counts = qs.aggregate(
            completed=Count("id", filter=Q(status=TransactionStatus.COMPLETED)),
            pending=Count("id", filter=Q(status__in=[
                TransactionStatus.SUBMITTED, TransactionStatus.UNDER_REVIEW,
                TransactionStatus.VERIFIED, TransactionStatus.ON_HOLD,
                TransactionStatus.PKR_SENT,
            ])),
            rejected=Count("id", filter=Q(status=TransactionStatus.REJECTED)),
        )

        # Time series grouped by day
        time_series_rows = list(
            qs.values("tx_date")
            .annotate(
                pkr=Sum("net_pkr", filter=Q(status=TransactionStatus.COMPLETED)),
                completed=Count("id", filter=Q(status=TransactionStatus.COMPLETED)),
                pending=Count("id", filter=Q(status__in=[
                    TransactionStatus.SUBMITTED, TransactionStatus.UNDER_REVIEW,
                    TransactionStatus.VERIFIED, TransactionStatus.ON_HOLD,
                    TransactionStatus.PKR_SENT,
                ])),
                rejected=Count("id", filter=Q(status=TransactionStatus.REJECTED)),
                total_amount=Sum("amount", filter=~Q(status=TransactionStatus.REJECTED)),
                count=Count("id"),
            )
            .order_by("tx_date")
        )
        time_series = [
            {
                "date": str(r["tx_date"]),
                "pkr": float(r["pkr"] or 0),
                "completed": r["completed"],
                "pending": r["pending"],
                "rejected": r["rejected"],
                "count": r["count"],
            }
            for r in time_series_rows
        ]

        if is_customer:
            return Response({
                "total_count": total_count,
                "submitted_total": float(submitted_total),
                "submitted_by_currency": submitted_by_currency,
                "gross_pkr_received": float(gross_pkr_received),
                "transferred": float(transferred),
                "pending": status_counts["pending"] or 0,
                "completed": status_counts["completed"] or 0,
                "rejected": status_counts["rejected"] or 0,
                "time_series": time_series,
            })

        fee_pkr = completed_qs.aggregate(
            v=Sum(F("fee_amount_foreign") * F("exchange_rate"), output_field=DecimalField(max_digits=20, decimal_places=2))
        )["v"] or Decimal("0")

        rate_spread_pkr = completed_qs.filter(
            real_exchange_rate__gt=F("exchange_rate")
        ).aggregate(
            v=Sum(F("amount") * (F("real_exchange_rate") - F("exchange_rate")), output_field=DecimalField(max_digits=20, decimal_places=2))
        )["v"] or Decimal("0")

        fee_charged_pkr = fee_pkr
        total_company_pkr = fee_charged_pkr + rate_spread_pkr

        # Top 20 customers (staff only)
        top_cust_rows = list(
            non_rejected
            .values("customer_id", "customer__full_name", "customer__email")
            .annotate(
                received=Sum("amount"),
                transferred=Sum("net_pkr", filter=Q(status=TransactionStatus.COMPLETED)),
                profit=Sum(
                    F("fee_amount_foreign") * F("exchange_rate"),
                    filter=Q(status=TransactionStatus.COMPLETED),
                    output_field=DecimalField(max_digits=20, decimal_places=2)
                ),
                count=Count("id"),
            )
            .order_by("-received")[:20]
        )
        top_customers = [
            {
                "id": str(r["customer_id"]),
                "name": r["customer__full_name"] or r["customer__email"] or str(r["customer_id"])[:8],
                "email": r["customer__email"] or "",
                "value": float(r["received"] or 0),
                "transferred": float(r["transferred"] or 0),
                "profit": float(r["profit"] or 0),
                "count": r["count"],
            }
            for r in top_cust_rows
        ]

        return Response({
            "total_count": total_count,
            "submitted_total": float(submitted_total),
            "submitted_by_currency": submitted_by_currency,
            "gross_pkr_received": float(gross_pkr_received),
            "transferred": float(transferred),
            "fee_pkr": float(fee_pkr),
            "fee_charged_pkr": float(fee_charged_pkr),
            "rate_spread_pkr": float(rate_spread_pkr),
            "total_company_pkr": float(total_company_pkr),
            "pending": status_counts["pending"] or 0,
            "completed": status_counts["completed"] or 0,
            "rejected": status_counts["rejected"] or 0,
            "top_customers": top_customers,
            "time_series": time_series,
        })

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
                | Q(customer__full_name__icontains=search)
                | Q(customer__email__icontains=search)
            )
        # Date range — inclusive of both bounds, calendar-day semantics.
        # Filters on the TRANSACTION date (occurred_on, falling back to
        # the entry date for legacy rows) — same semantics as the list.
        from django.db.models.functions import Coalesce, TruncDate
        from django.db.models import DateField
        qs = qs.annotate(
            tx_date=Coalesce(
                "occurred_on", TruncDate("created_at"),
                output_field=DateField(),
            ),
        )
        try:
            df = request.query_params.get("date_from")
            dt_ = request.query_params.get("date_to")
            date_by = (request.query_params.get("date_by") or "tx_date").lower()
            field_name = "created_at__date" if date_by in ("submission", "created_at") else "tx_date"

            if df:
                qs = qs.filter(**{f"{field_name}__gte": datetime.strptime(df, "%Y-%m-%d").date()})
            if dt_:
                qs = qs.filter(**{f"{field_name}__lte": datetime.strptime(dt_, "%Y-%m-%d").date()})
        except (ValueError, TypeError):
            pass

        qs = qs.order_by("-tx_date", "-created_at")

        # Build the CSV in memory. For the data sets this app handles
        # (low thousands per customer), in-memory is fine. If we ever
        # need to handle millions of rows we'd swap to the streaming
        # `csv.writer` + generator pattern.
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Reference",
            "Date",
            "Submitted at",
            "Customer name",
            "Customer email",
            "Sender name",
            "Sender company",
            "Sender bank",
            "External tx ID",
            "Currency",
            "Amount",
            "Customer Rate (Tangent)",
            "Actual Rate (Real)",
            "Fee %",
            "Fee (foreign)",
            "Net (foreign)",
            "Gross PKR",
            "Net PKR",
            "Rate Spread Profit (PKR)",
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
            # Compute rate spread profit for this row
            try:
                rate_spread_profit = str(tx.compute_rate_spread_profit())
            except Exception:
                rate_spread_profit = "0.00"
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
                s(tx.real_exchange_rate),
                s(tx.fee_percentage),
                s(tx.fee_amount_foreign),
                s(tx.net_amount_foreign),
                s(tx.gross_pkr),
                s(tx.net_pkr),
                rate_spread_profit,
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
        profile = CustomerProfile.objects.only("kyc_status").filter(user=request.user).first()
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
        validated = dict(s.validated_data)
        # Business date defaults to today when the client didn't send one.
        if not validated.get("occurred_on"):
            validated["occurred_on"] = timezone.localdate()
        with dbtx.atomic():
            ref = next_reference(IncomingPayment, prefix="PBX")
            payment = IncomingPayment.objects.create(
                customer=request.user,
                reference=ref,
                status=TransactionStatus.SUBMITTED,
                **validated,
            )
            # Stamp a provisional dollar rate so weekly reports have a PKR
            # figure to show immediately, before the accountant processes it.
            # Never overrides a real rate; see Utils/default_rate.py.
            if apply_default_rate(payment):
                payment.save(update_fields=[
                    "exchange_rate", "gross_pkr", "is_rate_provisional",
                    "updated_at",
                ])
            _record_status_change(
                payment, from_status="", to_status=TransactionStatus.SUBMITTED,
                user=request.user, note="Submitted by customer",
            )
            # After commit: the alert links straight to the review screen,
            # which would 404 if the row weren't visible yet.
            dbtx.on_commit(lambda: _notify_admins_new_payment(payment))
        return Response(
            IncomingPaymentSerializer(payment).data,
            status=status.HTTP_201_CREATED,
        )

    # ---- accountant: verify documents (STAGE 1) ----
    @action(
        detail=False, methods=["post"], url_path="staff-create",
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
    )
    def staff_create(self, request):
        """Admin/accountant creates a payment ON BEHALF OF a customer.

        Used when a customer doesn't enter their own transactions — staff
        record them from the dashboard. Mirrors the customer `create` flow
        but: (a) the customer is taken from the `customer` field in the body
        rather than request.user, (b) no KYC gate (staff are trusted), and
        (c) `handled_by` records which staff member entered it.

        Body: customer (user id) + the usual payment fields
              (payment_method, sender_name, sender_company,
               external_transaction_id, amount, currency, screenshot_*).
        """
        customer_id = request.data.get("customer")
        if not customer_id:
            return Response(
                {"customer": "Select a customer to record this payment for."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from myapp.Models.Auth_models import User
        customer = User.objects.filter(
            pk=customer_id, role=UserRole.CUSTOMER,
        ).first()
        if not customer:
            return Response(
                {"customer": "No such customer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = IncomingPaymentCreateSerializer(
            data=request.data, context={"request": request},
        )
        s.is_valid(raise_exception=True)
        validated = dict(s.validated_data)
        # Business date — staff may backdate a batch; defaults to today.
        if not validated.get("occurred_on"):
            validated["occurred_on"] = timezone.localdate()
        with dbtx.atomic():
            ref = next_reference(IncomingPayment, prefix="PBX")
            payment = IncomingPayment.objects.create(
                customer=customer,
                reference=ref,
                status=TransactionStatus.SUBMITTED,
                handled_by=request.user,
                **validated,
            )
            # Same provisional-rate stamp as the customer flow.
            if apply_default_rate(payment):
                payment.save(update_fields=[
                    "exchange_rate", "gross_pkr", "is_rate_provisional",
                    "updated_at",
                ])
            _record_status_change(
                payment, from_status="", to_status=TransactionStatus.SUBMITTED,
                user=request.user,
                note=f"Submitted on behalf of {customer.email} by {request.user.email}",
            )
            staff_email = request.user.email
            dbtx.on_commit(
                lambda: _notify_admins_new_payment(
                    payment, submitted_by=staff_email,
                )
            )
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_CREATE, target=payment,
            description=(
                f"Recorded payment {payment.reference} on behalf of "
                f"{customer.email} ({payment.amount} "
                f"{getattr(payment.currency, 'code', '') or ''})"
            ),
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
            # Store the real/actual rate; default to tangent rate if not provided
            payment.real_exchange_rate = s.validated_data.get(
                "real_exchange_rate"
            ) or s.validated_data["exchange_rate"]
            payment.fee_percentage = s.validated_data["fee_percentage"]
            # A human has now supplied the real rate — the auto-assigned
            # placeholder (if any) is superseded, so the row stops being
            # reported as an estimate.
            payment.is_rate_provisional = False
            # Store fee allocation override (for under-fee transactions)
            payment.fee_allocation = s.validated_data.get("fee_allocation")
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
            # beat task already flagged it. `stale_at` is cleared too so a
            # payment that somehow re-enters PKR_SENT starts a fresh clock
            # rather than inheriting the old one and auto-confirming at once.
            payment.is_stale = False
            payment.stale_at = None
            payment.save(update_fields=[
                "customer_confirmed_at", "completed_at",
                "status", "is_stale", "stale_at", "updated_at",
            ])
            _record_status_change(
                payment, before_status, TransactionStatus.COMPLETED,
                user=request.user,
                note=("Customer confirmed receipt"
                      + (f" — {note}" if note else "")),
            )
            # After commit: fee distribution below runs in the same block,
            # and staff shouldn't be told the books closed until they have.
            dbtx.on_commit(
                lambda: _notify_admins_payment_confirmed(payment, confirmed_by=note)
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
            payment.stale_at = None
            payment.save(update_fields=[
                "force_completed_by", "force_completed_at", "completed_at",
                "status", "is_stale", "stale_at", "updated_at",
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

    @action(
        detail=False, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdmin],
        url_path="bulk-force-complete",
    )
    def bulk_force_complete(self, request):
        """
        Force-complete MANY stale PKR_SENT payments in one request.

        Body:
            {
              "ids": ["<uuid>", "<uuid>", ...],   # required, non-empty
              "reason": "<text>"                  # required, applied to all
            }

        Each payment is processed in its own savepoint so one bad row
        (e.g. already completed, or wrong status) doesn't roll back the
        whole batch. Returns a per-id breakdown of what succeeded and what
        was skipped, plus the updated rows.

        Mirrors the single `force_complete` action exactly: same status
        guard, same fee distribution, same status-history + audit logging,
        and the same required-reason rule — just looped over a list.
        """
        from myapp.Utils.partner_ledger import distribute_fee_for_payment

        ids = request.data.get("ids")
        reason = (request.data.get("reason") or "").strip()

        if not isinstance(ids, (list, tuple)) or not ids:
            return Response(
                {"detail": "Provide a non-empty list of payment ids in `ids`."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not reason:
            return Response(
                {"detail": "A reason is required and is logged against every "
                           "payment in the batch."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # De-dupe while preserving order.
        seen = set()
        ordered_ids = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                ordered_ids.append(i)

        payments = {
            str(p.id): p
            for p in IncomingPayment.objects.filter(id__in=ordered_ids)
        }

        completed = []
        skipped = []

        for pid in ordered_ids:
            payment = payments.get(str(pid))
            if payment is None:
                skipped.append({"id": str(pid), "reason": "Not found."})
                continue
            if payment.status != TransactionStatus.PKR_SENT:
                skipped.append({
                    "id": str(pid),
                    "reference": payment.reference,
                    "reason": "Not awaiting confirmation (status is "
                              f"'{payment.status}').",
                })
                continue
            try:
                with dbtx.atomic():
                    before_status = payment.status
                    payment.force_completed_by = request.user
                    payment.force_completed_at = timezone.now()
                    payment.completed_at = timezone.now()
                    payment.status = TransactionStatus.COMPLETED
                    payment.is_stale = False
                    payment.stale_at = None
                    payment.save(update_fields=[
                        "force_completed_by", "force_completed_at",
                        "completed_at", "status", "is_stale", "stale_at",
                        "updated_at",
                    ])
                    _record_status_change(
                        payment, before_status, TransactionStatus.COMPLETED,
                        user=request.user,
                        note=f"Force-completed by admin (bulk) — reason: {reason}",
                    )
                    distribute_fee_for_payment(payment)
                    AuditLog.record(
                        user=request.user, action=AuditLog.ACTION_UPDATE,
                        target=payment,
                        description=(
                            f"{payment.reference}: force-completed by admin "
                            f"(bulk). Reason: {reason}"
                        ),
                    )
                completed.append(payment)
            except Exception as e:  # pragma: no cover — keep the batch going
                skipped.append({
                    "id": str(pid),
                    "reference": getattr(payment, "reference", ""),
                    "reason": f"Error: {e}",
                })

        return Response({
            "completed_count": len(completed),
            "skipped_count": len(skipped),
            "completed": IncomingPaymentSerializer(completed, many=True).data,
            "skipped": skipped,
        })

    # ---- admin/accountant: update real exchange rate on existing transaction ----
    @action(
        detail=True, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="update-real-rate",
    )
    def update_real_rate(self, request, pk=None):
        """
        Update the actual/real exchange rate on an existing transaction.
        
        Only real_exchange_rate is editable post-transfer — the tangent rate
        (exchange_rate) that was communicated to the customer cannot be changed.
        All company rate-spread profit calculations update automatically.
        Also re-distributes partner fee if payment is completed.
        """
        from myapp.serializers.Transaction_serializers import UpdateRealRateSerializer
        from myapp.Utils.partner_ledger import redistribute_fee_for_payment

        payment = self.get_object()
        s = UpdateRealRateSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        new_real_rate = s.validated_data["real_exchange_rate"]

        # Validate: real rate must be >= tangent rate
        if payment.exchange_rate and new_real_rate < payment.exchange_rate:
            return Response(
                {"detail": (
                    "Actual rate cannot be less than the customer (tangent) rate. "
                    f"Tangent rate is {payment.exchange_rate}."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_real_rate = payment.real_exchange_rate
        with dbtx.atomic():
            payment.real_exchange_rate = new_real_rate
            payment.save(update_fields=["real_exchange_rate", "updated_at"])

            # If payment is completed, update partner ledger entries
            # with new rate_spread_profit_pkr values
            if payment.status == TransactionStatus.COMPLETED:
                redistribute_fee_for_payment(payment, update_rate_only=True)

            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                target=payment,
                description=(
                    f"{payment.reference}: real exchange rate updated "
                    f"{old_real_rate} → {new_real_rate}"
                ),
                before={"real_exchange_rate": str(old_real_rate)},
                after={"real_exchange_rate": str(new_real_rate)},
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
        .prefetch_related("receipts")
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

        if payment.status != TransactionStatus.VERIFIED:
            raise ValidationError({"incoming_payment": f"Payment must be VERIFIED. Current status is {payment.status}."})

        # --- Gate (item 10): rate + fee MUST be applied before recording PKR
        # transfer. This is enforced server-side so any client that skips the
        # UI block still can't bypass it. ---
        if not payment.is_rate_fee_applied:
            raise ValidationError({
                "incoming_payment":
                "Apply exchange rate and fee before recording the PKR transfer.",
            })

        uploaded_files = []
        if hasattr(request, "FILES"):
            uploaded_files = (
                request.FILES.getlist("receipts")
                or request.FILES.getlist("receipts[]")
                or request.FILES.getlist("receipt")
            )
        if len(uploaded_files) > 5:
            raise ValidationError({
                "receipts": "A maximum of 5 receipts can be uploaded per PKR transfer.",
            })

        with dbtx.atomic():
            # Ensure payment hasn't been completed concurrently
            payment = IncomingPayment.objects.select_for_update().get(pk=payment.pk)
            if payment.status != TransactionStatus.VERIFIED:
                raise ValidationError({"incoming_payment": f"Payment must be VERIFIED. Current status is {payment.status}."})

            data_to_create = dict(s.validated_data)
            if uploaded_files:
                data_to_create["receipt"] = uploaded_files[0]

            before_status = payment.status
            ref = next_reference(OutgoingPKRTransfer, prefix="OUT")
            transfer = OutgoingPKRTransfer.objects.create(
                reference=ref,
                sent_by=request.user,
                **data_to_create,
            )
            for f in uploaded_files:
                OutgoingPKRTransferReceipt.objects.create(transfer=transfer, file=f)

            # Stop at PKR_SENT and wait for the customer to confirm receipt.
            # The customer portal shows an "I received my PKR" button; when
            # they click it, status flips to COMPLETED and partner fees are
            # distributed (see the `customer_confirm` action). If they never
            # respond, `flag_stale_payments` surfaces it in the Awaiting
            # Customer Confirmation queue after `stale_payment_minutes`, and
            # `auto_confirm_stale_payments` closes it out on their behalf
            # `auto_confirm_payment_minutes` later.
            payment.status = TransactionStatus.PKR_SENT
            payment.handled_by = request.user
            # A fresh PKR_SENT episode must start with a clean staleness
            # clock, otherwise a re-sent payment inherits an old `stale_at`
            # and is auto-confirmed on the very next beat run.
            payment.is_stale = False
            payment.stale_at = None
            payment.save(update_fields=[
                "status", "handled_by", "is_stale", "stale_at", "updated_at",
            ])
            _record_status_change(
                payment, before_status, TransactionStatus.PKR_SENT,
                user=request.user, note=f"PKR sent — ref {transfer.reference}",
            )

        return Response(
            OutgoingTransferSerializer(transfer).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=False, methods=["post"],
        permission_classes=[IsAuthenticated, IsAdminOrAccountant],
        url_path="bulk",
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def bulk(self, request):
        """
        Record ONE PKR transfer covering several of a customer's payments.

        Body: payment_ids[], customer_bank_account, amount_pkr,
              bank_transaction_id, notes, receipt (optional file).

        All selected payments must belong to the SAME customer, share one
        currency, and be ready (rate+fee applied, not already settled).
        Every payment is linked to the single transfer and flipped to
        PKR_SENT together — the customer then sees the same bank
        transaction ID + receipt on each of them.
        """
        from myapp.serializers.Transaction_serializers import (
            OutgoingTransferBulkCreateSerializer,
        )

        # payment_ids may arrive as a JSON list or as repeated form fields.
        data = request.data
        if hasattr(data, "getlist"):
            data = data.copy()
            ids = request.data.getlist("payment_ids") or request.data.getlist("payment_ids[]")
            if ids:
                data.setlist("payment_ids", ids)
            receipt_files = request.FILES.getlist("receipts") or request.FILES.getlist("receipts[]")
            if receipt_files:
                data.setlist("receipts", receipt_files)

        s = OutgoingTransferBulkCreateSerializer(data=data)
        s.is_valid(raise_exception=True)
        vd = s.validated_data
        ids = [str(x) for x in vd["payment_ids"]]

        payments = list(
            IncomingPayment.objects.filter(id__in=ids).select_related("customer")
        )
        found_ids = {str(p.id) for p in payments}
        missing = [i for i in ids if i not in found_ids]
        if missing:
            raise ValidationError({
                "payment_ids": f"Unknown payment id(s): {', '.join(missing)}.",
            })

        # ── Same customer ────────────────────────────────────────────────
        customers = {str(p.customer_id) for p in payments}
        if len(customers) != 1:
            raise ValidationError({
                "payment_ids":
                "All selected payments must belong to the same customer.",
            })

        # ── Same currency ────────────────────────────────────────────────
        # `currency` is a FK with to_field="code", so currency_id IS the
        # currency code string on the model instance (currency_code only
        # exists on the serializer, not the model).
        currencies = {p.currency_id for p in payments}
        if len(currencies) != 1:
            raise ValidationError({
                "payment_ids":
                "All selected payments must share the same currency.",
            })

        # ── All ready, none already settled ──────────────────────────────
        not_ready = [p.reference for p in payments if not p.is_rate_fee_applied]
        if not_ready:
            raise ValidationError({
                "payment_ids":
                "Apply exchange rate and fee before recording the PKR "
                f"transfer. Not ready: {', '.join(not_ready)}.",
            })
        not_verified = [
            p.reference for p in payments
            if p.status != TransactionStatus.VERIFIED
        ]
        if not_verified:
            raise ValidationError({
                "payment_ids":
                f"All payments must be VERIFIED. Invalid: {', '.join(not_verified)}.",
            })

        uploaded_files = []
        if hasattr(request, "FILES"):
            uploaded_files = (
                request.FILES.getlist("receipts")
                or request.FILES.getlist("receipts[]")
                or request.FILES.getlist("receipt")
            )
        if len(uploaded_files) > 5:
            raise ValidationError({
                "receipts": "A maximum of 5 receipts can be uploaded per PKR transfer.",
            })
        primary_receipt = uploaded_files[0] if uploaded_files else vd.get("receipt")

        with dbtx.atomic():
            # Refresh all payments to ensure none were completed concurrently
            payment_ids = sorted(p.pk for p in payments)
            locked_payments = list(
                IncomingPayment.objects
                .select_for_update()
                .filter(pk__in=payment_ids)
                .order_by("pk")
            )
            
            invalid = [
                p for p in locked_payments
                if p.status != TransactionStatus.VERIFIED
            ]

            if invalid:
                invalid_refs = ", ".join(p.reference for p in invalid)
                raise ValidationError({
                    "payment_ids": f"Payments must be VERIFIED. Invalid: {invalid_refs}."
                })
            
            payments = locked_payments

            ref = next_reference(OutgoingPKRTransfer, prefix="OUT")
            transfer = OutgoingPKRTransfer.objects.create(
                reference=ref,
                sent_by=request.user,
                incoming_payment=None,
                customer_bank_account=vd["customer_bank_account"],
                amount_pkr=vd["amount_pkr"],
                bank_transaction_id=vd["bank_transaction_id"],
                notes=vd.get("notes", "") or "",
                receipt=primary_receipt,
            )
            transfer.payments.set(payments)
            for f in uploaded_files:
                OutgoingPKRTransferReceipt.objects.create(transfer=transfer, file=f)

            for payment in payments:
                before_status = payment.status
                # Same as the single-payment path: stop at PKR_SENT, let the
                # customer confirm (or the auto-confirm task close it out),
                # and distribute partner fees at true completion only.
                payment.status = TransactionStatus.PKR_SENT
                payment.handled_by = request.user
                payment.is_stale = False
                payment.stale_at = None
                payment.save(update_fields=[
                    "status", "handled_by", "is_stale", "stale_at", "updated_at",
                ])

                _record_status_change(
                    payment, before_status, TransactionStatus.PKR_SENT,
                    user=request.user,
                    note=f"PKR sent (bulk) — ref {transfer.reference}",
                )

            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_CREATE,
                target=transfer,
                description=(
                    f"{transfer.reference}: bulk PKR transfer covering "
                    f"{len(payments)} payment(s) for customer "
                    f"{payments[0].customer.email}."
                ),
            )

        return Response(
            OutgoingTransferSerializer(transfer).data,
            status=status.HTTP_201_CREATED,
        )
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
          # defer() the two resubmit-diff fields added in migration 0033.
          # This lets the view work even if the migration columns are missing
          # from the DB (e.g. a fresh restore or a migration that was recorded
          # but not actually applied). We never display these fields here anyway.
          .defer(
              "profile__kyc_last_resubmit_at",
              "profile__kyc_last_resubmit_changes",
          )
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
