"""Per-customer fee configuration — admin only for writes, customers can read own rate."""
from decimal import Decimal
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Auth_models import UserRole
from myapp.Models.Core_models import SystemSetting
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


def _system_default_fee():
    """Fetch the platform-wide default fee percentage."""
    try:
        s = SystemSetting.objects.filter(key="default_fee_percentage").first()
        if s and s.value:
            return Decimal(str(s.value))
    except Exception:
        pass
    return Decimal("5.00")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_effective_fee(request):
    """
    Returns the effective fee percentage for the current user.

    - If the user has a CustomerFeeConfig override, return that.
    - Otherwise return the system default.
    - Customers and staff both get a useful response.
    """
    u = request.user
    source = "system_default"
    percentage = _system_default_fee()
    notes = ""

    try:
        cfg = CustomerFeeConfig.objects.filter(customer=u).first()
        if cfg:
            percentage = Decimal(str(cfg.fee_percentage))
            source = "customer_override"
            notes = cfg.notes or ""
    except Exception:
        pass

    return Response({
        "fee_percentage": str(percentage),
        "source": source,   # 'customer_override' | 'system_default'
        "notes": notes,
        "role": u.role,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def customer_effective_fee(request, user_id):
    """Admin utility: get the effective fee % a specific customer pays."""
    from myapp.Models.Auth_models import User
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response({"detail": "Not found"}, status=404)

    percentage = _system_default_fee()
    source = "system_default"
    notes = ""
    cfg = CustomerFeeConfig.objects.filter(customer=user).first()
    if cfg:
        percentage = Decimal(str(cfg.fee_percentage))
        source = "customer_override"
        notes = cfg.notes or ""

    return Response({
        "user": str(user.id),
        "email": user.email,
        "fee_percentage": str(percentage),
        "source": source,
        "notes": notes,
    })
