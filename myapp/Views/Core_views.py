"""
Core views: currencies, system settings, audit log, dashboard summary.
"""
from decimal import Decimal
from django.db.models import Sum, Count, Q
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.generics import ListAPIView

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Auth_models import UserRole
from myapp.Models.Core_models import Currency, SystemSetting, PaymentMethod
from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus
from myapp.serializers.Core_serializers import (
    CurrencySerializer, SystemSettingSerializer, AuditLogSerializer,
)
from myapp.serializers.Transaction_serializers import PaymentMethodSerializer
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant


class CurrencyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = Currency.objects.all()
    serializer_class = CurrencySerializer
    pagination_class = None

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdmin()]


class PaymentMethodViewSet(viewsets.ModelViewSet):
    """
    Admin-managed list of payment methods (Zelle, Cash App, ACH/Wire, etc.).

    - GET list/detail: any authenticated user (customer sees active ones on
      the New Payment form; the client filters for is_active=True).
    - POST / PATCH / DELETE: admin only.

    `?active_only=true` filters to active methods (used by customer UI).
    """
    queryset = PaymentMethod.objects.all().order_by("sort_order", "label")
    serializer_class = PaymentMethodSerializer
    lookup_field = "code"
    pagination_class = None

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdmin()]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("active_only") in ("1", "true", "True", "yes"):
            qs = qs.filter(is_active=True)
        return qs


class SystemSettingViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = SystemSetting.objects.all().order_by("key")
    serializer_class = SystemSettingSerializer
    lookup_field = "key"
    pagination_class = None

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user)


class AuditLogListView(ListAPIView):
    """
    Full activity feed.
    Query params:
      - target_model   filter by model class name (e.g. 'IncomingPayment')
      - target_id      filter by target PK (string match)
      - user           filter by actor user id
      - action         filter by action verb
      - q              free-text search over description + target_label + user email
    """
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    serializer_class = AuditLogSerializer
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user").order_by("-created_at")
        p = self.request.query_params
        if p.get("target_model"):
            qs = qs.filter(target_model=p["target_model"])
        if p.get("target_id"):
            qs = qs.filter(target_id=p["target_id"])
        if p.get("user"):
            qs = qs.filter(user_id=p["user"])
        if p.get("action"):
            qs = qs.filter(action=p["action"])
        q = p.get("q")
        if q:
            qs = qs.filter(
                Q(description__icontains=q) |
                Q(target_label__icontains=q) |
                Q(user__email__icontains=q)
            )
        return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    u = request.user

    if u.role == UserRole.CUSTOMER:
        qs = IncomingPayment.objects.filter(customer=u)
    else:
        qs = IncomingPayment.objects.all()

    by_currency = {}
    for row in qs.values("currency_id").annotate(
        total_amount=Sum("amount"),
        total_net=Sum("net_amount_foreign"),
        count=Count("id"),
    ):
        by_currency[row["currency_id"]] = {
            "total_received": str(row["total_amount"] or 0),
            "total_net": str(row["total_net"] or 0),
            "count": row["count"],
        }

    totals = qs.aggregate(
        total_count=Count("id"),
        total_pkr=Sum("net_pkr"),
        pending=Count("id", filter=Q(status__in=[
            TransactionStatus.SUBMITTED,
            TransactionStatus.UNDER_REVIEW,
        ])),
        awaiting_pkr=Count("id", filter=Q(status=TransactionStatus.VERIFIED)),
        completed=Count("id", filter=Q(status=TransactionStatus.COMPLETED)),
        rejected=Count("id", filter=Q(status=TransactionStatus.REJECTED)),
    )

    return Response({
        "role": u.role,
        "totals": {
            "transactions": totals["total_count"] or 0,
            "total_pkr_received": str(totals["total_pkr"] or Decimal("0")),
            "pending": totals["pending"] or 0,
            "awaiting_pkr_transfer": totals["awaiting_pkr"] or 0,
            "completed": totals["completed"] or 0,
            "rejected": totals["rejected"] or 0,
        },
        "by_currency": by_currency,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def bank_balances(request):
    """
    Admin/accountant view of our money across currencies.

    Business flow:
      - Customer sends us X USD → it arrives in our USD bank account.
      - We convert externally and send PKR out of our PKR bank account.
      - We DO NOT send USD back out of the USD account.

    So for each foreign currency the full received `amount` stays on hand
    until we manually withdraw/transfer it out (which is not tracked in the
    app yet, hence only received – rejected counts as on-hand here).

    Per foreign currency we return:
        received          = sum of `amount` on all non-rejected payments
        on_hand_est       = received (everything received stays in our bank)
        fees_collected    = sum of `fee_amount_foreign` on completed payments
                            — this is the profit portion we retain in this currency
        disbursed_foreign = sum of `amount` on completed payments where the
                            foreign was "matched" by outgoing PKR (informational)
        pkr_disbursed     = sum of `net_pkr` on completed payments (PKR we paid out)
        live_rate         = latest stored rate, if any
        on_hand_pkr       = on_hand_est × live_rate (quick PKR comparison)
        profit_pkr        = fees_collected × live_rate (our fee earnings in PKR)
    """
    if request.user.role not in (UserRole.ADMIN, UserRole.ACCOUNTANT):
        return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

    from myapp.Models.Rate_models import ExchangeRate

    rates = {}
    for r in ExchangeRate.objects.all():
        rates[r.currency_id] = Decimal(r.rate_to_pkr or 0)

    per_currency = {}
    active_codes = list(
        Currency.objects.filter(is_active=True, is_base=False).values_list("code", flat=True)
    )
    for code in active_codes:
        base_qs = IncomingPayment.objects.filter(currency_id=code).exclude(
            status=TransactionStatus.REJECTED,
        )
        agg = base_qs.aggregate(
            received=Sum("amount"),
            count=Count("id"),
        )
        completed_qs = IncomingPayment.objects.filter(
            currency_id=code, status=TransactionStatus.COMPLETED,
        )
        completed_agg = completed_qs.aggregate(
            matched_foreign=Sum("amount"),
            fees=Sum("fee_amount_foreign"),
            pkr_out=Sum("net_pkr"),
            count=Count("id"),
        )
        received = Decimal(agg["received"] or 0)
        matched_foreign = Decimal(completed_agg["matched_foreign"] or 0)
        fees_collected = Decimal(completed_agg["fees"] or 0)
        pkr_disbursed = Decimal(completed_agg["pkr_out"] or 0)

        # Foreign cash we still physically hold in this currency's bank account.
        # Everything received stays with us until withdrawn externally — the
        # customer is paid in PKR, not in foreign currency.
        on_hand_est = received

        live_rate = rates.get(code) or Decimal(0)
        on_hand_pkr = (on_hand_est * live_rate).quantize(Decimal("0.01")) if live_rate else Decimal(0)
        profit_pkr = (fees_collected * live_rate).quantize(Decimal("0.01")) if live_rate else Decimal(0)

        per_currency[code] = {
            "received":           str(received),
            "disbursed_foreign":  str(matched_foreign),  # informational only
            "fees_collected":     str(fees_collected),
            "pkr_disbursed":      str(pkr_disbursed),
            "on_hand_est":        str(on_hand_est.quantize(Decimal("0.01"))),
            "on_hand_pkr":        str(on_hand_pkr),
            "profit_pkr":         str(profit_pkr),
            "live_rate":          str(live_rate) if live_rate else None,
            "received_count":     agg["count"] or 0,
            "completed_count":    completed_agg["count"] or 0,
        }

    # PKR side: total disbursed + total fees (in PKR)
    pkr_out_total = IncomingPayment.objects.filter(
        status=TransactionStatus.COMPLETED,
    ).aggregate(total=Sum("net_pkr"))["total"] or Decimal(0)

    total_fees_pkr_estimate = sum(
        Decimal(c["profit_pkr"]) for c in per_currency.values()
    )
    total_on_hand_pkr = sum(
        Decimal(c["on_hand_pkr"]) for c in per_currency.values()
    )

    return Response({
        "currencies": per_currency,
        "pkr_disbursed_total":   str(pkr_out_total),
        "total_on_hand_pkr":     str(total_on_hand_pkr),
        "total_profit_pkr_est":  str(total_fees_pkr_estimate),
    })
