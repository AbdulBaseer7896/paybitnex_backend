"""
Admin management of vendor-portal access.

This is where "make a customer a vendor" happens. Three operations:

  POST   /internal-transactions/vendors/<id>/grant-portal/
  POST   /internal-transactions/vendors/<id>/revoke-portal/
  GET    /internal-transactions/vendors/portal-candidates/

DESIGN DECISION — vendor users keep role='customer'
---------------------------------------------------
A vendor is a customer who *also* has vendor access, exactly as
described: "that is also a customer but a vendor customer". They are NOT
given a new `UserRole`.

Reasons this matters beyond tidiness:

  1. Introducing a fourth role would mean auditing every existing
     `role == 'customer'` check in the codebase — there are many, across
     permissions, serializers, guards and the payment-creation gate. Any
     one missed becomes a silent access change for a live account.
  2. A vendor may legitimately still be a trading customer, submitting
     their own payments. A separate role would take that away.

Access is therefore granted by the `Vendor.portal_user` link plus the
`portal_enabled` switch — additive, and revocable without touching the
user's account.

GRANTING IS DELIBERATELY EXPLICIT
---------------------------------
Linking is one-to-one in both directions and enforced here:
  - a user already linked to another vendor is rejected;
  - a vendor already linked to another user is rejected unless the
    caller passes `replace=true`.

Silently re-pointing a link would move visibility of a set of financial
records from one person to another with no trace. The audit log records
every grant, replacement and revocation.
"""
from django.db import transaction as dbtx
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Auth_models import User, UserRole
from myapp.Models.InternalTx_models import Vendor
from myapp.Utils.permissions import IsAdmin


def _vendor_or_404(vendor_id):
    return Vendor.objects.select_related("portal_user").filter(
        pk=vendor_id,
    ).first()


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def portal_candidates(request):
    """GET .../vendors/portal-candidates/?q=

    Customers eligible to be linked to a vendor: active, role=customer,
    and not already linked to some other vendor.
    """
    q = (request.query_params.get("q") or "").strip()

    taken = set(
        Vendor.objects
        .exclude(portal_user__isnull=True)
        .values_list("portal_user_id", flat=True)
    )

    qs = User.objects.filter(role=UserRole.CUSTOMER, is_active=True)
    if taken:
        qs = qs.exclude(pk__in=taken)
    if q:
        from django.db.models import Q
        for token in q.split():
            qs = qs.filter(
                Q(email__icontains=token) | Q(full_name__icontains=token)
            )
    qs = qs.order_by("full_name", "email")[:50]

    return Response({
        "results": [
            {
                "id": str(u.id),
                "email": u.email,
                "full_name": u.full_name or "",
            }
            for u in qs
        ],
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def grant_portal(request, pk=None):
    """POST .../vendors/<id>/grant-portal/

    Body:
        user     — UUID of an existing customer to link, OR
        email    — email of an existing customer, OR
        create   — {email, full_name, phone?} to provision a NEW customer
                   account and link it in one step.
        replace  — bool; required to re-point a vendor already linked.

    Returns the vendor plus, when a new account was created, a
    `temporary_password` for the admin to hand over.
    """
    vendor = _vendor_or_404(pk)
    if vendor is None:
        return Response({"detail": "Vendor not found."}, status=404)

    replace = str(request.data.get("replace") or "").lower() in ("1", "true", "yes")
    create_spec = request.data.get("create") or None
    user_id = (request.data.get("user") or "").strip()
    email = (request.data.get("email") or "").strip()

    temp_password = None
    target = None

    # ── Resolve or create the account ────────────────────────────────
    if create_spec:
        new_email = (create_spec.get("email") or "").strip().lower()
        if not new_email:
            return Response(
                {"create": "An email is required to create the account."},
                status=400,
            )
        if User.objects.filter(email__iexact=new_email).exists():
            return Response(
                {"create": f"A user with {new_email} already exists. "
                           f"Link that account instead of creating one."},
                status=400,
            )
        temp_password = User.objects.make_random_password(length=12)
        target = User.objects.create_user(
            email=new_email,
            password=temp_password,
            full_name=(create_spec.get("full_name") or vendor.name or "").strip(),
            phone=(create_spec.get("phone") or "").strip(),
            role=UserRole.CUSTOMER,
            is_active=True,
            created_by=request.user,
        )
        target._plain_password = temp_password
    elif user_id:
        target = User.objects.filter(pk=user_id).first()
    elif email:
        target = User.objects.filter(email__iexact=email).first()

    if target is None:
        return Response(
            {"detail": "Provide `user`, `email`, or a `create` block."},
            status=400,
        )

    # ── Guards ───────────────────────────────────────────────────────
    if target.role not in (UserRole.CUSTOMER,):
        return Response(
            {"user": f"Only customer accounts can be given vendor access "
                     f"(this account is '{target.role}'). Staff already see "
                     f"all vendor data."},
            status=400,
        )
    if not target.is_active:
        return Response(
            {"user": "That account is deactivated. Reactivate it first."},
            status=400,
        )

    clash = Vendor.objects.filter(portal_user=target).exclude(pk=vendor.pk).first()
    if clash is not None:
        return Response(
            {"user": f"{target.email} is already the portal account for "
                     f"vendor '{clash.name}'. One account maps to one vendor."},
            status=400,
        )

    previous = vendor.portal_user
    if previous and previous.pk != target.pk and not replace:
        return Response(
            {
                "detail": f"'{vendor.name}' is already linked to "
                          f"{previous.email}. Re-send with replace=true to "
                          f"move access to {target.email}.",
                "current_user": {
                    "id": str(previous.pk), "email": previous.email,
                },
            },
            status=409,
        )

    # ── Apply ────────────────────────────────────────────────────────
    with dbtx.atomic():
        vendor.portal_user = target
        vendor.portal_enabled = True
        vendor.portal_granted_at = timezone.now()
        vendor.portal_granted_by = request.user
        vendor.save(update_fields=[
            "portal_user", "portal_enabled",
            "portal_granted_at", "portal_granted_by", "updated_at",
        ])

    if previous and previous.pk != target.pk:
        desc = (f"Vendor portal for '{vendor.name}' MOVED from "
                f"{previous.email} to {target.email}")
    else:
        desc = (f"Vendor portal access granted to {target.email} "
                f"for '{vendor.name}'"
                + (" (new account created)" if temp_password else ""))
    AuditLog.record(
        user=request.user, action=AuditLog.ACTION_UPDATE, target=vendor,
        description=desc,
    )

    return Response({
        "vendor": {
            "id": str(vendor.id),
            "name": vendor.name,
            "portal_enabled": vendor.portal_enabled,
            "portal_granted_at": vendor.portal_granted_at,
        },
        "user": {
            "id": str(target.id),
            "email": target.email,
            "full_name": target.full_name or "",
        },
        "temporary_password": temp_password,
        "replaced": bool(previous and previous.pk != target.pk),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def revoke_portal(request, pk=None):
    """POST .../vendors/<id>/revoke-portal/

    Body: unlink — bool. Default False turns the switch off but keeps the
    link (so access can be restored in one click). True clears the link.

    The user's own account is never deactivated here — they may still be
    a trading customer.
    """
    vendor = _vendor_or_404(pk)
    if vendor is None:
        return Response({"detail": "Vendor not found."}, status=404)

    if not vendor.portal_user_id and not vendor.portal_enabled:
        return Response(
            {"detail": "This vendor has no portal access to revoke."},
            status=400,
        )

    unlink = str(request.data.get("unlink") or "").lower() in ("1", "true", "yes")
    who = getattr(vendor.portal_user, "email", "—")

    with dbtx.atomic():
        vendor.portal_enabled = False
        fields = ["portal_enabled", "updated_at"]
        if unlink:
            vendor.portal_user = None
            vendor.portal_granted_at = None
            vendor.portal_granted_by = None
            fields += ["portal_user", "portal_granted_at", "portal_granted_by"]
        vendor.save(update_fields=fields)

    AuditLog.record(
        user=request.user, action=AuditLog.ACTION_UPDATE, target=vendor,
        description=(
            f"Vendor portal access {'revoked and unlinked' if unlink else 'disabled'} "
            f"for '{vendor.name}' ({who})"
        ),
    )

    return Response({
        "vendor": {
            "id": str(vendor.id),
            "name": vendor.name,
            "portal_enabled": False,
            "portal_user": None if unlink else (
                str(vendor.portal_user_id) if vendor.portal_user_id else None
            ),
        },
        "unlinked": unlink,
    })
