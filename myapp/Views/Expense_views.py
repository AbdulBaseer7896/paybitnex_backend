"""
Expense views — admin + accountant can view and create expenses;
admin can edit and delete. Every mutation is audit-logged.
New: distributions endpoint allows splitting an expense across
     partners and/or company with custom amounts.
"""
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import transaction as dbtx
from django.db.models import Sum, Count, Q

from myapp.Models.Expense_models import Expense, ExpenseDistribution
from myapp.Models.Partner_models import Partner
from myapp.Models.Audit_models import AuditLog
from myapp.serializers.Expense_serializers import (
    ExpenseSerializer, ExpenseDistributionSerializer,
    ExpenseDistributionBulkSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant


class ExpenseViewSet(viewsets.ModelViewSet):
    """
    GET    /expenses/                    - list
    POST   /expenses/                    - create
    GET    /expenses/{id}/               - detail
    PATCH  /expenses/{id}/               - update (admin only)
    DELETE /expenses/{id}/               - delete (admin only)
    GET    /expenses/summary/            - aggregated totals
    GET    /expenses/{id}/distributions/ - list distributions
    POST   /expenses/{id}/distributions/ - set distributions (atomic replace)
    GET    /expenses/total-pkr/          - total expenses in PKR
    """
    queryset = Expense.objects.select_related(
        "currency", "created_by",
    ).prefetch_related("distributions__partner")
    serializer_class = ExpenseSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["category", "currency", "created_by"]
    search_fields = ["title", "vendor", "purpose"]
    ordering_fields = ["spent_on", "created_at", "amount"]
    ordering = ["-spent_on", "-created_at"]

    def get_permissions(self):
        if self.action in ("update", "partial_update", "destroy"):
            return [IsAuthenticated(), IsAdmin()]
        return [IsAuthenticated(), IsAdminOrAccountant()]

    def get_queryset(self):
        qs = super().get_queryset()
        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(spent_on__gte=date_from)
        if date_to:
            qs = qs.filter(spent_on__lte=date_to)
        return qs

    def perform_create(self, serializer):
        obj = serializer.save(created_by=self.request.user)
        AuditLog.record(
            user=self.request.user,
            action="expense.create",
            target=obj,
            metadata={
                "title": obj.title, "category": obj.category,
                "amount": str(obj.amount), "currency": obj.currency_id,
                "spent_on": str(obj.spent_on),
            },
        )

    def perform_update(self, serializer):
        before = {
            "title": serializer.instance.title,
            "category": serializer.instance.category,
            "amount": str(serializer.instance.amount),
            "currency": serializer.instance.currency_id,
            "spent_on": str(serializer.instance.spent_on),
        }
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action="expense.update", target=obj,
            metadata={"before": before, "after": {
                "title": obj.title, "category": obj.category,
                "amount": str(obj.amount), "currency": obj.currency_id,
                "spent_on": str(obj.spent_on),
            }},
        )

    def perform_destroy(self, instance):
        snapshot = {
            "id": str(instance.id), "title": instance.title,
            "category": instance.category, "amount": str(instance.amount),
            "currency": instance.currency_id, "spent_on": str(instance.spent_on),
        }
        AuditLog.record(
            user=self.request.user, action="expense.delete",
            target=instance, metadata=snapshot,
        )
        instance.delete()

    # ── Distribution sub-resource ──────────────────────────────────────────
    @action(detail=True, methods=["get", "post"], url_path="distributions",
            permission_classes=[IsAuthenticated, IsAdminOrAccountant])
    def distributions(self, request, pk=None):
        expense = self.get_object()

        if request.method == "GET":
            qs = expense.distributions.select_related("partner").all()
            return Response(ExpenseDistributionSerializer(qs, many=True).data)

        # POST — atomic replace of all distributions
        if not request.user.role == "admin":
            return Response(
                {"detail": "Only admin can set distributions."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = ExpenseDistributionBulkSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        slices = ser.validated_data["distributions"]

        # Validate: partner UUIDs exist
        partner_ids = [
            s["partner"] for s in slices
            if s.get("partner") is not None
        ]
        if partner_ids:
            found = set(
                str(p) for p in
                Partner.objects.filter(id__in=partner_ids).values_list("id", flat=True)
            )
            missing = [str(pid) for pid in partner_ids if str(pid) not in found]
            if missing:
                return Response(
                    {"detail": f"Partner(s) not found: {', '.join(missing)}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Validate: total <= expense.amount (within 1 cent tolerance).
        # Partners can absorb 100% of an expense — no company slice required.
        # We only reject if the total EXCEEDS the expense amount.
        total = sum(Decimal(str(s["amount"])) for s in slices)
        if total > expense.amount + Decimal("0.01"):
            return Response(
                {
                    "detail": (
                        f"Distribution total {total} exceeds "
                        f"expense amount {expense.amount}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with dbtx.atomic():
            expense.distributions.all().delete()
            created = []
            for s in slices:
                partner_id = s.get("partner")
                d = ExpenseDistribution.objects.create(
                    expense=expense,
                    partner_id=partner_id,
                    amount=Decimal(str(s["amount"])),
                    updated_by=request.user,
                )
                created.append(d)

        AuditLog.record(
            user=request.user, action="expense.distribution.set",
            target=expense,
            metadata={
                "slices": [
                    {
                        "partner": str(s.get("partner") or "company"),
                        "amount": str(s["amount"]),
                    }
                    for s in slices
                ],
            },
        )
        return Response(
            ExpenseDistributionSerializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    # ── Summary ───────────────────────────────────────────────────────────
    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.filter_queryset(self.get_queryset())
        grouping_qs = qs.order_by()

        by_currency = list(
            grouping_qs.values("currency_id")
              .annotate(total=Sum("amount"), count=Count("id"))
              .order_by("currency_id")
        )
        by_category = list(
            grouping_qs.values("category")
              .annotate(count=Count("id"), total_pkr=Sum(
                  "amount", filter=Q(currency_id="PKR"),
              ))
              .order_by("category")
        )

        return Response({
            "total_count": qs.count(),
            "by_currency": [
                {"currency": r["currency_id"], "total": str(r["total"] or 0),
                 "count": r["count"]}
                for r in by_currency
            ],
            "by_category": [
                {"category": r["category"], "count": r["count"],
                 "total_pkr": str(r["total_pkr"] or 0)}
                for r in by_category
            ],
        })

    @action(detail=False, methods=["get"], url_path="total-pkr")
    def total_pkr(self, request):
        """
        Total expenses in PKR for the date window.
        Also returns per-partner deduction based on their expense distributions.
        """
        from myapp.Models.Rate_models import ExchangeRate

        qs = self.filter_queryset(self.get_queryset())
        rates = {"PKR": Decimal("1")}
        for r in ExchangeRate.objects.all():
            rates[r.currency_id] = Decimal(str(r.rate_to_pkr or 0))

        total = Decimal("0")
        breakdown = {}
        grouping_qs = qs.order_by().values("currency_id").annotate(total=Sum("amount"))
        for row in grouping_qs:
            code = row["currency_id"]
            amt = Decimal(str(row["total"] or 0))
            rate = rates.get(code) or Decimal("0")
            pkr = (amt * rate).quantize(Decimal("0.01"))
            total += pkr
            breakdown[code] = {
                "amount": str(amt), "rate": str(rate) if rate else None, "pkr": str(pkr),
            }

        # Per-partner expense deduction from distributions
        from myapp.Models.Expense_models import ExpenseDistribution
        partner_deductions = {}  # partner_id → PKR amount
        company_deduction = Decimal("0")

        dist_qs = ExpenseDistribution.objects.filter(
            expense__in=qs,
        ).select_related("expense__currency")
        for d in dist_qs:
            code = d.expense.currency_id
            rate = rates.get(code) or Decimal("0")
            pkr = (Decimal(str(d.amount)) * rate).quantize(Decimal("0.01"))
            if d.partner_id:
                pid = str(d.partner_id)
                partner_deductions[pid] = partner_deductions.get(pid, Decimal("0")) + pkr
            else:
                company_deduction += pkr

        return Response({
            "total_pkr": str(total.quantize(Decimal("0.01"))),
            "breakdown": breakdown,
            "partner_deductions": {k: str(v) for k, v in partner_deductions.items()},
            "company_deduction": str(company_deduction),
        })
