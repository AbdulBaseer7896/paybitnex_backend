"""
Banking views:
- PakistaniBankListView / ForeignBankListView: public lookups
  (available to any authenticated user so customer forms can populate).
- CustomerBankAccountViewSet: customer manages their PK bank accounts.
- CustomerMerchantAccountViewSet: customer manages their foreign accounts.
"""
from django.db import transaction as dbtx
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.generics import ListAPIView

from myapp.Models.Auth_models import UserRole
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


class _OwnerScopedMixin:
    """Restricts queryset to the requesting customer. Staff see all."""
    def get_queryset(self):
        qs = super().get_queryset()
        u = self.request.user
        if u.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            if customer_id := self.request.query_params.get("customer"):
                qs = qs.filter(customer_id=customer_id)
            return qs
        return qs.filter(customer=u)

    def perform_create(self, serializer):
        # Force customer to self for non-staff
        u = self.request.user
        if u.role == UserRole.CUSTOMER:
            serializer.save(customer=u)
        else:
            serializer.save()


class CustomerBankAccountViewSet(_OwnerScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = CustomerBankAccount.objects.select_related("bank").all()
    serializer_class = CustomerBankAccountSerializer

    @action(detail=True, methods=["post"])
    def make_primary(self, request, pk=None):
        acct = self.get_object()
        with dbtx.atomic():
            CustomerBankAccount.objects.filter(
                customer=acct.customer,
            ).update(is_primary=False)
            acct.is_primary = True
            acct.save(update_fields=["is_primary"])
        return Response(self.get_serializer(acct).data)


class CustomerMerchantAccountViewSet(_OwnerScopedMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = CustomerMerchantAccount.objects.select_related("bank").all()
    serializer_class = CustomerMerchantAccountSerializer

    @action(detail=True, methods=["post"])
    def make_primary(self, request, pk=None):
        acct = self.get_object()
        with dbtx.atomic():
            CustomerMerchantAccount.objects.filter(
                customer=acct.customer,
            ).update(is_primary=False)
            acct.is_primary = True
            acct.save(update_fields=["is_primary"])
        return Response(self.get_serializer(acct).data)
