"""
Banking views — customer manages their PK + foreign bank accounts.

Every mutation (create/update/delete/make-primary) is audit-logged so the
admin can see exactly what the customer changed and when.
"""
from django.db import transaction as dbtx
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.generics import ListAPIView

from myapp.Models.Auth_models import UserRole
from myapp.Models.Audit_models import AuditLog
from myapp.Models.Banking_models import (
    PakistaniBank, ForeignBank,
    CustomerBankAccount, CustomerMerchantAccount,
)
from myapp.serializers.Banking_serializers import (
    PakistaniBankSerializer, ForeignBankSerializer,
    CustomerBankAccountSerializer, CustomerMerchantAccountSerializer,
)


class PakistaniBankListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    queryset = PakistaniBank.objects.filter(is_active=True)
    serializer_class = PakistaniBankSerializer
    pagination_class = None


class ForeignBankListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    queryset = ForeignBank.objects.filter(is_active=True)
    serializer_class = ForeignBankSerializer
    pagination_class = None
    filterset_fields = ["country"]


# Fields we snapshot in audit logs
_BANK_SNAPSHOT_FIELDS = [
    "bank_id", "holder_name", "account_number", "iban",
    "is_primary", "is_active",
]
_MERCHANT_SNAPSHOT_FIELDS = [
    "bank_id", "holder_name", "account_number", "iban",
    "routing_number", "swift_code", "is_primary", "is_active",
]


def _snapshot(instance, fields):
    """Build a dict snapshot of an instance's audit-relevant fields."""
    out = {}
    for f in fields:
        val = getattr(instance, f, None)
        # Cast UUIDs and other non-JSON-native types to strings
        if val is not None and not isinstance(val, (str, int, float, bool, list, dict)):
            val = str(val)
        out[f] = val
    return out


def _diff(before, after):
    """Return only fields that actually changed between two snapshots."""
    return {
        k: {"from": before.get(k), "to": after.get(k)}
        for k in after
        if before.get(k) != after.get(k)
    }


class _OwnerScopedAuditedMixin:
    """
    Queryset scoping + audit logging for account CRUD.
    Subclasses must set `snapshot_fields` and `acct_label` class attrs.
    """
    snapshot_fields = []
    acct_label = "Account"

    def get_queryset(self):
        qs = super().get_queryset()
        u = self.request.user
        if u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            if customer_id := self.request.query_params.get("customer"):
                qs = qs.filter(customer_id=customer_id)
            return qs
        return qs.filter(customer=u)

    # ---- CREATE ----
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        u = request.user
        if u.role == UserRole.CUSTOMER:
            instance = serializer.save(customer=u)
        else:
            instance = serializer.save()
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_CREATE, target=instance,
            description=f"{self.acct_label} added: {instance}",
            after=_snapshot(instance, self.snapshot_fields),
        )
        return Response(self.get_serializer(instance).data,
                        status=status.HTTP_201_CREATED)

    # ---- UPDATE ----
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        before = _snapshot(instance, self.snapshot_fields)

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        instance.refresh_from_db()

        after = _snapshot(instance, self.snapshot_fields)
        changes = _diff(before, after)
        if changes:
            # Format a human-readable description
            change_desc = ", ".join(
                f"{k}: {v['from']!r} → {v['to']!r}" for k, v in changes.items()
            )
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE, target=instance,
                description=f"{self.acct_label} updated ({instance}): {change_desc}",
                before=before, after=after,
            )
        return Response(self.get_serializer(instance).data)

    # ---- DELETE ----
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        before = _snapshot(instance, self.snapshot_fields)
        label = str(instance)
        instance.delete()
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_DELETE,
            target_label=label,
            description=f"{self.acct_label} removed: {label}",
            before=before,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerBankAccountViewSet(_OwnerScopedAuditedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = CustomerBankAccount.objects.select_related("bank").all()
    serializer_class = CustomerBankAccountSerializer
    snapshot_fields = _BANK_SNAPSHOT_FIELDS
    acct_label = "PKR bank account"

    @action(detail=True, methods=["post"])
    def make_primary(self, request, pk=None):
        acct = self.get_object()
        with dbtx.atomic():
            # Un-set any other primary for this customer
            CustomerBankAccount.objects.filter(
                customer=acct.customer,
            ).exclude(pk=acct.pk).update(is_primary=False)
            was_primary = acct.is_primary
            acct.is_primary = True
            acct.is_active = True  # re-activate if it was soft-disabled
            acct.save(update_fields=["is_primary", "is_active", "updated_at"])
        if not was_primary:
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE, target=acct,
                description=f"PKR bank account set as primary: {acct}",
                before={"is_primary": False}, after={"is_primary": True},
            )
        return Response(self.get_serializer(acct).data)


class CustomerMerchantAccountViewSet(_OwnerScopedAuditedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = CustomerMerchantAccount.objects.select_related("bank").all()
    serializer_class = CustomerMerchantAccountSerializer
    snapshot_fields = _MERCHANT_SNAPSHOT_FIELDS
    acct_label = "Merchant (foreign) account"

    @action(detail=True, methods=["post"])
    def make_primary(self, request, pk=None):
        acct = self.get_object()
        with dbtx.atomic():
            CustomerMerchantAccount.objects.filter(
                customer=acct.customer,
            ).exclude(pk=acct.pk).update(is_primary=False)
            was_primary = acct.is_primary
            acct.is_primary = True
            acct.is_active = True
            acct.save(update_fields=["is_primary", "is_active", "updated_at"])
        if not was_primary:
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE, target=acct,
                description=f"Merchant account set as primary: {acct}",
                before={"is_primary": False}, after={"is_primary": True},
            )
        return Response(self.get_serializer(acct).data)
