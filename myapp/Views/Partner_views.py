"""
Partner views — admin-only for mutations.
Partners themselves don't log in; they're just profit-share records.

Endpoints:
    GET  /partners/                   → list partners
    POST /partners/                   → create partner (+ optional initial share)
    GET  /partners/{id}/              → detail
    PATCH/partners/{id}/              → update
    DELETE /partners/{id}/            → deactivate (soft)

    POST /partners/shares/bulk/       → re-balance all shares atomically
    GET  /partners/{id}/ledger/       → ledger entries for a partner
    GET  /partners/{id}/balance/      → running balance (PKR + per currency)
"""
from decimal import Decimal

from django.db import transaction as dbtx
from django.db.models import Sum
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Partner_models import Partner, PartnerShare, PartnerLedgerEntry
from myapp.serializers.Partner_serializers import (
    PartnerSerializer, PartnerCreateSerializer, PartnerShareSerializer,
    PartnerLedgerEntrySerializer, SharesBulkUpdateSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Utils.partner_ledger import partner_balance


class PartnerViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = Partner.objects.all().select_related("share").order_by("name")
    serializer_class = PartnerSerializer
    search_fields = ["name", "email"]
    filterset_fields = ["is_active"]

    def get_serializer_class(self):
        if self.action == "create":
            return PartnerCreateSerializer
        return PartnerSerializer

    def create(self, request, *args, **kwargs):
        s = PartnerCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        share_pct = s.validated_data.pop("share_percentage", Decimal("0"))
        with dbtx.atomic():
            partner = Partner.objects.create(
                created_by=request.user, **s.validated_data,
            )
            PartnerShare.objects.create(
                partner=partner, percentage=share_pct, updated_by=request.user,
            )
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_CREATE, target=partner,
                description=f"Created partner {partner.name} @ {share_pct}%",
            )
        return Response(
            PartnerSerializer(partner).data, status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        partner = self.get_object()
        partner.is_active = False
        partner.save(update_fields=["is_active", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_DELETE, target=partner,
            description=f"Deactivated partner {partner.name}",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        partner = self.get_object()
        qs = PartnerLedgerEntry.objects.filter(partner=partner).select_related("payment")
        page = self.paginate_queryset(qs)
        ser = PartnerLedgerEntrySerializer(page or qs, many=True)
        if page is not None:
            return self.get_paginated_response(ser.data)
        return Response(ser.data)

    @action(detail=True, methods=["get"])
    def balance(self, request, pk=None):
        partner = self.get_object()
        per_currency = (
            PartnerLedgerEntry.objects
            .filter(partner=partner)
            .values("currency_code")
            .annotate(total=Sum("amount_foreign"))
        )
        return Response({
            "partner": str(partner.id),
            "total_pkr": str(partner_balance(partner, "PKR")),
            "by_currency": {
                r["currency_code"]: str(r["total"]) for r in per_currency
            },
        })

    @action(
        detail=False, methods=["post"], url_path="shares/bulk",
        permission_classes=[IsAuthenticated, IsAdmin],
    )
    def bulk_update_shares(self, request):
        s = SharesBulkUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        results = []
        with dbtx.atomic():
            for item in s.validated_data["shares"]:
                pct = Decimal(str(item["percentage"]))
                share, _ = PartnerShare.objects.select_for_update().get_or_create(
                    partner_id=item["partner"],
                    defaults={"percentage": pct, "updated_by": request.user},
                )
                share.percentage = pct
                share.updated_by = request.user
                share.save(update_fields=["percentage", "updated_by", "updated_at"])
                results.append(PartnerShareSerializer(share).data)
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                description=f"Bulk share update: {len(results)} partners",
                after={"shares": [dict(r) for r in results]},
            )
        return Response({"updated": len(results), "shares": results})


class PartnerLedgerListView(viewsets.ReadOnlyModelViewSet):
    """All ledger entries across partners (admin / accountant)."""
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    queryset = (
        PartnerLedgerEntry.objects
        .select_related("partner", "payment").order_by("-created_at")
    )
    serializer_class = PartnerLedgerEntrySerializer
    filterset_fields = ["partner", "currency_code", "payment"]
