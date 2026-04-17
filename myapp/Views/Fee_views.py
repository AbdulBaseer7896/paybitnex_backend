"""Per-customer fee configuration — admin only."""
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from myapp.Models.Fee_models import CustomerFeeConfig
from myapp.serializers.Fee_serializers import CustomerFeeConfigSerializer
from myapp.Utils.permissions import IsAdmin


class CustomerFeeConfigViewSet(viewsets.ModelViewSet):
    """
    Admin can set/override fee % per customer.
    If no config exists for a customer, the system-default from
    SystemSetting('default_fee_percentage') applies.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    queryset = CustomerFeeConfig.objects.select_related("customer").all()
    serializer_class = CustomerFeeConfigSerializer
    filterset_fields = ["customer"]

    def perform_create(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
