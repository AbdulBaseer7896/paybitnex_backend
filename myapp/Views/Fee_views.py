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
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant


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
@permission_classes([IsAuthenticated, IsAdminOrAccountant])
def customer_effective_fee(request, user_id):
    """
    Get the effective fee % a specific customer pays.

    Used by the accountant's "Apply Rate & Fee" form to prefill
    the percentage field, AND by admin tooling. Both roles need
    this — accountants are the ones primarily approving payments,
    so requiring IsAdmin alone caused the form to silently fall
    back to "Required" with no prefill.
    """
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

    pct_str = str(percentage)
    return Response({
        "user": str(user.id),
        "email": user.email,
        # Both keys returned for compatibility — `fee_percentage`
        # is the legacy name, `effective_percentage` is what the
        # accountant frontend ApplyRateFeeCard reads. Returning a
        # number-typed value (not just str) so React's <Input
        # type="number"> binds cleanly without a manual cast.
        "fee_percentage": pct_str,
        "effective_percentage": float(percentage),
        # Frontend short-form check: it tests `source === 'override'`
        # for the "Customer rate" badge label. Map our internal
        # 'customer_override' → 'override' here so the UI label
        # matches reality.
        "source": "override" if source == "customer_override" else "default",
        "notes": notes,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def partner_shares_info(request):
    """
    GET /fees/partner-shares-info/
    
    Returns total active partner shares % and per-partner breakdown.
    Used by the Apply Rate & Fee form to warn about under-fee conditions.
    Admin/accountant use only.
    """
    from myapp.Models.Partner_models import Partner, PartnerShare
    from decimal import Decimal

    partners = list(
        Partner.objects.filter(is_active=True)
        .select_related("share")
        .order_by("name")
    )
    
    partner_list = []
    total_pct = Decimal("0")
    for p in partners:
        share = getattr(p, "share", None)
        pct = Decimal(str(share.percentage)) if share else Decimal("0")
        total_pct += pct
        partner_list.append({
            "id": str(p.id),
            "name": p.name,
            "share_percentage": str(pct),
        })
    
    return Response({
        "total_partner_percentage": str(total_pct),
        "partners": partner_list,
    })
