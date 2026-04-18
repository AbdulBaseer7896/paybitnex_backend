"""
Expense views — admin + accountant can view and create expenses;
admin can edit and delete. Every mutation is audit-logged.
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Sum, Count, Q

from myapp.Models.Expense_models import Expense
from myapp.Models.Audit_models import AuditLog
from myapp.serializers.Expense_serializers import ExpenseSerializer
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant


class ExpenseViewSet(viewsets.ModelViewSet):
    """
    GET    /expenses/           - list (admin/accountant)
    POST   /expenses/           - create (admin/accountant)
    GET    /expenses/{id}/      - detail
    PATCH  /expenses/{id}/      - update (admin only)
    DELETE /expenses/{id}/      - delete (admin only)
    GET    /expenses/summary/   - aggregated totals per currency / category
    """
    queryset = Expense.objects.select_related("currency", "created_by")
    serializer_class = ExpenseSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["category", "currency", "created_by"]
    search_fields = ["title", "vendor", "purpose"]
    ordering_fields = ["spent_on", "created_at", "amount"]
    ordering = ["-spent_on", "-created_at"]

    def get_permissions(self):
        # Admin + accountant can read/create; admin-only to edit or delete
        if self.action in ("update", "partial_update", "destroy"):
            return [IsAuthenticated(), IsAdmin()]
        return [IsAuthenticated(), IsAdminOrAccountant()]

    def get_queryset(self):
        qs = super().get_queryset()
        # Optional date-range filters
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
                "title": obj.title,
                "category": obj.category,
                "amount": str(obj.amount),
                "currency": obj.currency_id,
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
            user=self.request.user,
            action="expense.update",
            target=obj,
            metadata={
                "before": before,
                "after": {
                    "title": obj.title,
                    "category": obj.category,
                    "amount": str(obj.amount),
                    "currency": obj.currency_id,
                    "spent_on": str(obj.spent_on),
                },
            },
        )

    def perform_destroy(self, instance):
        snapshot = {
            "id": str(instance.id),
            "title": instance.title,
            "category": instance.category,
            "amount": str(instance.amount),
            "currency": instance.currency_id,
            "spent_on": str(instance.spent_on),
        }
        AuditLog.record(
            user=self.request.user,
            action="expense.delete",
            target=instance,
            metadata=snapshot,
        )
        instance.delete()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Aggregated totals for the expenses page — total spend per currency,
        count per category, and overall count. Respects the same date-range
        filters as the list endpoint.
        """
        qs = self.filter_queryset(self.get_queryset())

        by_currency = list(
            qs.values("currency_id")
              .annotate(total=Sum("amount"), count=Count("id"))
              .order_by("currency_id")
        )

        by_category = list(
            qs.values("category")
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
