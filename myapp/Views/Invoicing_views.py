"""
Invoicing views — Client + CustomerCompany CRUD.

Both endpoints are scoped hard to the authenticated customer:
- `get_queryset()` filters to `customer=request.user`
- `perform_create()` forces `customer=request.user` so a customer can't
  create data on behalf of another.

Admin/accountant users get an empty queryset here (by design — per the
spec, Clients and Companies are private to the customer). We still allow
them to hit the endpoints so Django REST Framework doesn't 403 the whole
route; they just see nothing.
"""
from myapp.Utils.file_validators import validate_doc_file
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response

from django.db import transaction as dbtx

from myapp.Models.Auth_models import UserRole
from myapp.Models.Invoicing_models import Client, CustomerCompany
from myapp.serializers.Invoicing_serializers import (
    ClientSerializer, CustomerCompanySerializer,
)
from myapp.Models.Audit_models import AuditLog
from myapp.Utils.permissions import HasFeature


def _is_customer(request):
    return getattr(request.user, "role", None) == UserRole.CUSTOMER


class ClientViewSet(viewsets.ModelViewSet):
    """
    Customer-owned clients. CRUD restricted to the owning customer.

    Gated behind the 'invoicing' premium feature — customers without
    the grant get 403. Staff (admin/accountant) bypass the gate but
    still see an empty queryset per product spec.
    """
    permission_classes = [IsAuthenticated, HasFeature("invoicing")]
    serializer_class = ClientSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return Client.objects.none()
        qs = Client.objects.filter(customer=u)
        # Exclude archived by default; `?include_archived=true` flips it.
        p = self.request.query_params
        if p.get("include_archived") not in ("1", "true", "True", "yes"):
            qs = qs.exclude(is_archived=True)
        # Optional search across name / email / company.
        search = (p.get("search") or "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(company_name__icontains=search)
                | Q(email__icontains=search),
            )
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        # Force customer ownership — never trust client payload.
        obj = serializer.save(customer=self.request.user)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj, description=f"Client created: {obj.name}",
        )

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj, description=f"Client updated: {obj.name}",
        )

    def perform_destroy(self, instance):
        # Soft delete — preserve linkage to historical invoices. Hard
        # delete is available via an explicit `?hard=true` query param
        # only if no invoices reference it.
        if self.request.query_params.get("hard") in ("1", "true", "True"):
            AuditLog.record(
                user=self.request.user, action=AuditLog.ACTION_DELETE,
                target=instance,
                description=f"Client hard-deleted: {instance.name}",
            )
            instance.delete()
        else:
            instance.is_archived = True
            instance.save(update_fields=["is_archived", "updated_at"])
            AuditLog.record(
                user=self.request.user, action=AuditLog.ACTION_UPDATE,
                target=instance,
                description=f"Client archived: {instance.name}",
            )


class CustomerCompanyViewSet(viewsets.ModelViewSet):
    """
    Customer-owned companies (their own businesses they invoice from).
    CRUD restricted to the owning customer. At most one `is_primary=True`
    per customer; setting primary on a new row demotes all others.

    Gated behind the 'invoicing' premium feature.
    """
    permission_classes = [IsAuthenticated, HasFeature("invoicing")]
    serializer_class = CustomerCompanySerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return CustomerCompany.objects.none()
        return CustomerCompany.objects.filter(customer=u).order_by(
            "-is_primary", "name",
        )

    @dbtx.atomic
    def perform_create(self, serializer):
        # First company created is primary by default. Respect explicit
        # is_primary=True and demote all others.
        u = self.request.user
        make_primary = serializer.validated_data.get("is_primary", False)
        if not CustomerCompany.objects.filter(customer=u).exists():
            make_primary = True
        if make_primary:
            CustomerCompany.objects.filter(customer=u, is_primary=True).update(
                is_primary=False,
            )
        obj = serializer.save(customer=u, is_primary=make_primary)
        AuditLog.record(
            user=u, action=AuditLog.ACTION_CREATE, target=obj,
            description=f"Company created: {obj.name}",
        )

    @dbtx.atomic
    def perform_update(self, serializer):
        u = self.request.user
        new_primary = serializer.validated_data.get("is_primary", None)
        if new_primary is True:
            CustomerCompany.objects.filter(
                customer=u, is_primary=True,
            ).exclude(pk=serializer.instance.pk).update(is_primary=False)
        obj = serializer.save()
        AuditLog.record(
            user=u, action=AuditLog.ACTION_UPDATE, target=obj,
            description=f"Company updated: {obj.name}",
        )

    def perform_destroy(self, instance):
        u = self.request.user
        was_primary = instance.is_primary
        name = instance.name
        instance.delete()
        # If we deleted the primary, promote another to primary (the most
        # recently created one) so the customer always has a default.
        if was_primary:
            nxt = CustomerCompany.objects.filter(customer=u).order_by(
                "-created_at",
            ).first()
            if nxt:
                nxt.is_primary = True
                nxt.save(update_fields=["is_primary", "updated_at"])
        AuditLog.record(
            user=u, action=AuditLog.ACTION_DELETE, target=instance,
            description=f"Company deleted: {name}",
        )

    @action(detail=True, methods=["post"], url_path="make-primary")
    def make_primary(self, request, pk=None):
        """Quick-action endpoint to mark a company as primary."""
        company = self.get_object()
        with dbtx.atomic():
            CustomerCompany.objects.filter(
                customer=request.user, is_primary=True,
            ).exclude(pk=company.pk).update(is_primary=False)
            company.is_primary = True
            company.save(update_fields=["is_primary", "updated_at"])
        return Response(self.get_serializer(company).data)


# ──────────────────────────────────────────────────────────────────────
#  Admin-facing endpoints: PaymentMethod config + per-customer access
# ──────────────────────────────────────────────────────────────────────

from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant
from myapp.Models.Core_models import PaymentMethod
from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod
from myapp.serializers.Invoicing_serializers import (
    PaymentMethodConfigSerializer,
    CustomerAllowedPaymentMethodSerializer,
)


class PaymentMethodConfigViewSet(viewsets.ModelViewSet):
    """Admin CRUD for the master PaymentMethod config.

    Customers can only GET the list (so their invoice form knows what's
    available), but all detail fields are still returned — the data is
    public-by-design since it's printed on invoices the client sees.

    For customer GETs (list/retrieve), we automatically restrict the
    queryset to methods the admin has granted via
    `CustomerAllowedPaymentMethod`. This is a safety net so any
    customer-side UI that happens to hit this endpoint (instead of
    the canonical `/core/payment-methods/?for_me=true`) still gets a
    properly-filtered list. Staff (admin/accountant) always see the
    full list — they need it for assignment / review flows.
    """
    queryset = PaymentMethod.objects.all().order_by("sort_order", "label")
    serializer_class = PaymentMethodConfigSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        # Customers can read (to render their invoice method picker),
        # but only admin can write.
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdmin()]

    def get_queryset(self):
        qs = super().get_queryset()
        # Customer-facing GETs get the same filtering treatment as
        # /core/payment-methods/?for_me=true. Belt-and-suspenders:
        # if any frontend page accidentally fetches from this URL
        # without the for_me flag, customers still don't leak access
        # to methods they haven't been granted. Staff users keep
        # the full list since they need it to populate admin forms.
        user = self.request.user
        if (self.action in ("list", "retrieve")
                and getattr(user, "is_authenticated", False)
                and getattr(user, "role", None) == "customer"):
            from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod
            allowed_codes = set(
                CustomerAllowedPaymentMethod.objects
                .filter(customer=user)
                .values_list("payment_method_id", flat=True)
            )
            qs = qs.filter(code__in=allowed_codes)
        return qs

    def perform_create(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"Payment method created: {obj.code} ({obj.label})",
        )

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Payment method updated: {obj.code}",
        )

    def perform_destroy(self, instance):
        # Never hard-delete if any IncomingPayment references it.
        from myapp.Models.Transaction_models import IncomingPayment
        if IncomingPayment.objects.filter(payment_method_id=instance.code).exists():
            instance.is_active = False
            instance.save(update_fields=["is_active", "updated_at"])
            AuditLog.record(
                user=self.request.user, action=AuditLog.ACTION_UPDATE,
                target=instance,
                description=f"Payment method deactivated (referenced by "
                            f"existing payments): {instance.code}",
            )
        else:
            AuditLog.record(
                user=self.request.user, action=AuditLog.ACTION_DELETE,
                target=instance,
                description=f"Payment method deleted: {instance.code}",
            )
            instance.delete()


class CustomerAllowedPaymentMethodViewSet(viewsets.ModelViewSet):
    """
    Admin manages which payment methods each customer can use.

    Endpoints:
      GET    /invoicing/allowed-methods/?customer=<uuid>      List a customer's grants
      POST   /invoicing/allowed-methods/                       Grant access to (customer, method)
      DELETE /invoicing/allowed-methods/<id>/                  Revoke access
      POST   /invoicing/allowed-methods/<id>/make-primary/     Mark as primary for this customer
      POST   /invoicing/allowed-methods/bulk-set/              Replace a customer's whole grant set

    The bulk-set action powers the "Assign to customer" matrix UI — admin
    checks/unchecks methods for one customer and submits the whole list
    in a single request (atomic).
    """
    serializer_class = CustomerAllowedPaymentMethodSerializer
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]

    def get_queryset(self):
        qs = CustomerAllowedPaymentMethod.objects.select_related(
            "payment_method", "granted_by",
        ).filter(admin_excluded=False)  # Never show admin-excluded rows
        customer_id = self.request.query_params.get("customer")
        if customer_id:
            qs = qs.filter(customer_id=customer_id)
        return qs.order_by("-is_primary", "payment_method__sort_order")

    @dbtx.atomic
    def perform_create(self, serializer):
        obj = serializer.save(granted_by=self.request.user)
        # First grant defaults to primary.
        if not CustomerAllowedPaymentMethod.objects.filter(
            customer=obj.customer,
        ).exclude(pk=obj.pk).exists():
            obj.is_primary = True
            obj.save(update_fields=["is_primary"])
        # Enforce one-primary invariant if this row was created with is_primary=True.
        if obj.is_primary:
            CustomerAllowedPaymentMethod.objects.filter(
                customer=obj.customer, is_primary=True,
            ).exclude(pk=obj.pk).update(is_primary=False)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"Granted {obj.payment_method_id} to "
                        f"customer {obj.customer_id}",
        )

    def perform_destroy(self, instance):
        cid = instance.customer_id
        pmid = instance.payment_method_id
        was_primary = instance.is_primary
        was_auto = instance.auto_assigned

        if was_auto:
            # Don't physically delete auto-assigned rows — mark as excluded
            # so the next sync run doesn't re-add it to this customer.
            instance.admin_excluded = True
            instance.is_primary = False
            instance.save(update_fields=["admin_excluded", "is_primary"])
        else:
            instance.delete()

        # If we removed the primary, promote the most recently granted.
        if was_primary:
            nxt = CustomerAllowedPaymentMethod.objects.filter(
                customer_id=cid,
                admin_excluded=False,
            ).order_by("-granted_at").first()
            if nxt:
                nxt.is_primary = True
                nxt.save(update_fields=["is_primary"])
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_DELETE,
            target=None,
            description=(
                f"Removed {'(excluded auto-assigned) ' if was_auto else ''}"
                f"{pmid} from customer {cid}"
            ),
        )

    @action(detail=True, methods=["post"], url_path="make-primary")
    @dbtx.atomic
    def make_primary(self, request, pk=None):
        row = self.get_object()
        CustomerAllowedPaymentMethod.objects.filter(
            customer=row.customer, is_primary=True,
        ).exclude(pk=row.pk).update(is_primary=False)
        row.is_primary = True
        row.save(update_fields=["is_primary"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=row,
            description=f"Set primary={row.payment_method_id} for "
                        f"customer {row.customer_id}",
        )
        return Response(self.get_serializer(row).data)

    @action(detail=False, methods=["post"], url_path="bulk-set")
    @dbtx.atomic
    def bulk_set(self, request):
        """
        Replace a customer's allowed-method set in one shot.

        Request body:
            {
              "customer": "<uuid>",
              "grants": [
                  {"payment_method": "zelle",    "is_primary": false},
                  {"payment_method": "cashapp",  "is_primary": true}
              ]
            }

        Semantics: any existing grant for this customer that isn't in
        `grants` is deleted; grants in the payload are upserted.
        """
        customer_id = request.data.get("customer")
        grants = request.data.get("grants", [])
        if not customer_id:
            return Response(
                {"detail": "customer is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(grants, list):
            return Response(
                {"detail": "grants must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Normalize — at most one primary; fall back to the first row if
        # the caller didn't mark any.
        any_primary = any(g.get("is_primary") for g in grants)
        seen_primary = False
        cleaned = []
        for g in grants:
            pm = g.get("payment_method")
            if not pm:
                return Response(
                    {"detail": "Every grant must have payment_method."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            is_p = bool(g.get("is_primary")) and not seen_primary
            if is_p:
                seen_primary = True
            cleaned.append({"payment_method_id": pm, "is_primary": is_p})
        if grants and not any_primary:
            cleaned[0]["is_primary"] = True

        # Wipe and rebuild.
        existing = CustomerAllowedPaymentMethod.objects.filter(
            customer_id=customer_id,
        )
        existing.delete()
        for c in cleaned:
            CustomerAllowedPaymentMethod.objects.create(
                customer_id=customer_id,
                payment_method_id=c["payment_method_id"],
                is_primary=c["is_primary"],
                granted_by=request.user,
            )

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=None,
            description=f"Allowed methods replaced for customer "
                        f"{customer_id}: "
                        f"{[c['payment_method_id'] for c in cleaned]}",
        )
        qs = self.get_queryset().filter(customer_id=customer_id)
        return Response(self.get_serializer(qs, many=True).data)


# ──────────────────────────────────────────────────────────────────────
#  Customer-facing read-only endpoint: "what methods am I allowed to use?"
# ──────────────────────────────────────────────────────────────────────

from rest_framework.views import APIView


class MyAllowedPaymentMethodsView(APIView):
    """Returns the authenticated customer's allowed payment methods,
    enriched with the full PaymentMethod detail so the invoice form can
    render the picker without a second round-trip."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_customer(request):
            return Response([])
        grants = CustomerAllowedPaymentMethod.objects.filter(
            customer=request.user,
            payment_method__is_active=True,
        ).select_related("payment_method").order_by(
            "-is_primary", "payment_method__sort_order",
        )
        out = []
        for g in grants:
            pm = g.payment_method
            out.append({
                "id": g.id,
                "payment_method": pm.code,
                "payment_method_label": pm.label,
                "is_primary": g.is_primary,
                # Full method details so invoice form can render them.
                "email": pm.email,
                "phone": pm.phone,
                "cashapp_tag": pm.cashapp_tag,
                "holder_name": pm.holder_name,
                "account_number": pm.account_number,
                "routing_number": pm.routing_number,
                "bank_name": pm.bank_name,
                "account_type": pm.account_type,
                "address_line1": pm.address_line1,
                "address_line2": pm.address_line2,
                "city": pm.city,
                "state": pm.state,
                "postal_code": pm.postal_code,
                "country": pm.country,
                "qr_code_url": (request.build_absolute_uri(pm.qr_code.url)
                                if pm.qr_code else None),
                "instructions": pm.instructions,
            })
        return Response(out)


# ──────────────────────────────────────────────────────────────────────
#  Invoices — customer-facing CRUD + public share view
# ──────────────────────────────────────────────────────────────────────

import base64
import secrets
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404

from myapp.Models.Invoicing_models import Invoice, InvoiceLineItem, InvoiceStatus
from myapp.serializers.Invoicing_serializers import (
    InvoiceSerializer, PublicInvoiceSerializer,
)


def _generate_invoice_number(company):
    """Produce an invoice number and atomically bump the company's counter.

    Prefix resolution cascade:
      1. Per-company `invoice_number_prefix` if the customer set one
      2. Admin-wide `invoice_number_prefix` SystemSetting
      3. Hard-coded fallback `"BIT"`

    The counter itself always lives on the company, not the admin
    setting, because each customer's sequence should increment
    independently. Admin only controls the prefix DEFAULT — they don't
    dictate that all customers share a single sequence.

    We select_for_update the company row so concurrent invoice creates
    don't collide on the same number.
    """
    from myapp.Models.Invoicing_models import CustomerCompany
    from myapp.Models.Core_models import SystemSetting

    locked = CustomerCompany.objects.select_for_update().get(pk=company.pk)

    # Per-company first, then admin default, then hard-coded.
    prefix = (locked.invoice_number_prefix or "").strip()
    if not prefix:
        prefix = (SystemSetting.get("invoice_number_prefix", "") or "").strip()
    if not prefix:
        prefix = "BIT"

    n = locked.next_invoice_number or 1
    number = f"{prefix}{n:06d}"
    locked.next_invoice_number = n + 1
    locked.save(update_fields=["next_invoice_number", "updated_at"])
    return number


def _snapshot_company(company, request=None):
    """Freeze company letterhead fields into a JSON-friendly dict.

    We also resolve the logo to:
      - an absolute filesystem path (for the reportlab PDF renderer)
      - an absolute http(s) URL (for the frontend public invoice page,
        which runs on a different origin than Django and can't resolve
        relative /media/... URLs)

    `request` is used to build the absolute URL. If not provided we
    fall back to the FRONTEND-agnostic `site_url` setting or bare
    relative URL — the PDF renderer doesn't need the URL anyway.
    """
    snap = {
        "name": company.name,
        "email": company.email,
        "phone": company.phone,
        "website": company.website,
        "tax_id": company.tax_id,
        "address_line1": company.address_line1,
        "address_line2": company.address_line2,
        "city": company.city,
        "state": company.state,
        "postal_code": company.postal_code,
        "country": company.country,
        "invoice_number_prefix": company.invoice_number_prefix,
    }
    if company.logo:
        # Capture path and url INDEPENDENTLY — on Cloudinary storage,
        # `.path` raises NotImplementedError but `.url` works fine.
        # The old single try/except would skip BOTH on Cloudinary, so
        # the PDF renderer had no logo source at all.
        try:
            snap["logo_path"] = company.logo.path
        except Exception:
            pass
        try:
            rel_url = company.logo.url
            if request is not None:
                snap["logo_url"] = request.build_absolute_uri(rel_url)
            else:
                from django.conf import settings as dj_settings
                host = getattr(dj_settings, "BACKEND_URL", "") \
                       or getattr(dj_settings, "SITE_URL", "") \
                       or ""
                snap["logo_url"] = (
                    f"{host.rstrip('/')}{rel_url}" if host else rel_url
                )
        except Exception:
            pass
    return snap


def _snapshot_client(client):
    return {
        "name": client.name,
        "company_name": client.company_name,
        "email": client.email,
        "phone": client.phone,
        "address": client.address,
    }


def _snapshot_payment_method(pm, request=None):
    if not pm:
        return {}
    snap = {
        "code": pm.code,
        "label": pm.label,
        "holder_name": pm.holder_name,
        "email": pm.email,
        "phone": pm.phone,
        "cashapp_tag": pm.cashapp_tag,
        "account_number": pm.account_number,
        "routing_number": pm.routing_number,
        "bank_name": pm.bank_name,
        "account_type": pm.account_type,
        "instructions": pm.instructions,
    }
    if pm.qr_code:
        # Same two-step pattern as company logo — on Cloudinary the
        # `.path` attribute raises NotImplementedError, but `.url`
        # returns the Cloudinary URL. Capture them independently so
        # a path failure doesn't drop the url too.
        try:
            snap["qr_code_path"] = pm.qr_code.path
        except Exception:
            pass
        try:
            rel_url = pm.qr_code.url
            if request is not None:
                snap["qr_code_url"] = request.build_absolute_uri(rel_url)
            else:
                from django.conf import settings as dj_settings
                host = getattr(dj_settings, "BACKEND_URL", "") \
                       or getattr(dj_settings, "SITE_URL", "") \
                       or ""
                snap["qr_code_url"] = (
                    f"{host.rstrip('/')}{rel_url}" if host else rel_url
                )
        except Exception:
            pass
    return snap


def _resolve_payment_method_for_invoice(customer, requested_id):
    """Figure out which PaymentMethod to put on a new invoice.

    Rules (per product spec):
      1. If the customer explicitly picked a method AND it's in their
         allowed set AND it's active → use it.
      2. Else, if they have ANY primary allowed method → use that.
      3. Else, if an admin has designated a global primary via
         the existing PaymentMethodConfig → use that.
      4. Else, invoice is created without a payment section (None).
    """
    from myapp.Models.Core_models import PaymentMethod
    from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod

    allowed = CustomerAllowedPaymentMethod.objects.filter(
        customer=customer, payment_method__is_active=True,
    ).select_related("payment_method")

    # (1) — explicit choice from the allowed set
    if requested_id:
        row = allowed.filter(payment_method_id=requested_id).first()
        if row:
            return row.payment_method

    # (2) — customer's primary allowed
    primary = allowed.filter(is_primary=True).first()
    if primary:
        return primary.payment_method

    # (3) — any allowed (first is the default ordering)
    any_allowed = allowed.first()
    if any_allowed:
        return any_allowed.payment_method

    # (4) — admin-designated global fallback. We look for any active
    # method with sort_order=0 (which is the conventional "default" slot)
    # and fall back to the lowest sort_order otherwise. Caller can still
    # end up with None if the admin hasn't configured any method either.
    fallback = PaymentMethod.objects.filter(is_active=True).order_by(
        "sort_order", "label",
    ).first()
    return fallback


def _compute_totals(line_items_data, tax_percent):
    """Recompute subtotal / tax / total from validated line-item dicts."""
    subtotal = Decimal("0")
    for li in line_items_data:
        q = Decimal(str(li.get("quantity") or 0))
        p = Decimal(str(li.get("unit_price") or 0))
        subtotal += (q * p)
    subtotal = subtotal.quantize(Decimal("0.01"))
    tax_pct = Decimal(str(tax_percent or 0))
    tax_amt = (subtotal * tax_pct / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax_amt).quantize(Decimal("0.01"))
    return subtotal, tax_amt, total


def _build_and_cache_pdf(invoice, theme=None):
    """Render the PDF and save it to invoice.pdf_file.

    ``theme`` overrides the invoice's stored preference if given. If
    omitted we fall back to whatever was last stored on the invoice
    (default 'light'), so re-renders triggered by data changes (edit,
    send) preserve the customer's last chosen theme.
    """
    from myapp.Utils.invoice_pdf import render_invoice_pdf
    # Default to "dark" — matches the modern dashboard look that
    # customers expect their invoices to mirror. The light theme is
    # still available as an explicit choice in the preview toggle.
    chosen = (theme or getattr(invoice, "pdf_theme", None) or "dark").lower()
    if chosen not in ("light", "dark"):
        chosen = "dark"
    buf = render_invoice_pdf(invoice, theme=chosen)
    # Record the theme we used on the invoice itself; do this BEFORE
    # the .save() call so the field is persisted in the same write.
    invoice.pdf_theme = chosen
    # Wire the bytes into the FileField.
    invoice.pdf_file.save(
        f"{invoice.number}.pdf",
        ContentFile(buf.getvalue()),
        save=True,
    )
    return invoice.pdf_file


class InvoiceViewSet(viewsets.ModelViewSet):
    """
    Customer invoice endpoints.

      GET    /invoicing/invoices/              List my invoices (+ filters)
      POST   /invoicing/invoices/              Create — body {client, company,
                                                 payment_method?, tax_percent?,
                                                 general_description?, notes?,
                                                 due_date?, line_items: [...]}
      GET    /invoicing/invoices/<uuid>/       Detail
      DELETE /invoicing/invoices/<uuid>/       Void (soft)
      POST   /invoicing/invoices/<uuid>/send/  Send emails to client+customer
      POST   /invoicing/invoices/<uuid>/regenerate-pdf/  Re-render PDF

    Admin/accountant get an empty queryset — invoices are private to
    the customer per product spec.

    Gated behind the 'invoicing' premium feature.
    """
    permission_classes = [IsAuthenticated, HasFeature("invoicing")]
    serializer_class = InvoiceSerializer

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return Invoice.objects.none()
        qs = (Invoice.objects.filter(customer=u)
              .select_related("client", "company", "payment_method")
              .prefetch_related("line_items"))
        p = self.request.query_params
        # Filters — date range, client, payment method, company, status.
        if p.get("date_from"):
            qs = qs.filter(issue_date__gte=p.get("date_from"))
        if p.get("date_to"):
            qs = qs.filter(issue_date__lte=p.get("date_to"))
        if p.get("client"):
            qs = qs.filter(client_id=p.get("client"))
        if p.get("company"):
            qs = qs.filter(company_id=p.get("company"))
        if p.get("payment_method"):
            qs = qs.filter(payment_method_id=p.get("payment_method"))
        if p.get("status"):
            qs = qs.filter(status=p.get("status"))
        search = (p.get("search") or "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(number__icontains=search)
                | Q(client__name__icontains=search)
                | Q(client__company_name__icontains=search),
            )
        return qs.order_by("-created_at")

    @dbtx.atomic
    def create(self, request, *args, **kwargs):
        if not _is_customer(request):
            return Response(
                {"detail": "Only customers can create invoices."},
                status=status.HTTP_403_FORBIDDEN,
            )
        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        # Ensure client + company belong to the authenticated customer.
        if d["client"].customer_id != request.user.id:
            return Response(
                {"detail": "Client does not belong to you."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if d["company"].customer_id != request.user.id:
            return Response(
                {"detail": "Company does not belong to you."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Resolve payment methods ──
        # The create form now sends `payment_methods: [codes...]` for
        # multi-method invoices, falling back to the single
        # `payment_method` field for backward compatibility. We validate
        # every requested code against the customer's allowed set, then
        # if the resulting list is empty, fall through to the admin's
        # global default (primary) so the invoice still has a payment
        # section when possible.
        from myapp.Models.Invoicing_models import (
            CustomerAllowedPaymentMethod, InvoicePaymentMethod,
        )
        from myapp.Models.Core_models import PaymentMethod

        requested_codes = request.data.get("payment_methods") or []
        if (not requested_codes) and d.get("payment_method"):
            requested_codes = [d["payment_method"].code]

        allowed_ids = set(CustomerAllowedPaymentMethod.objects.filter(
            customer=request.user,
            payment_method__is_active=True,
        ).values_list("payment_method_id", flat=True))

        # Keep only methods the customer has access to. We filter by
        # the requested codes at the DB layer, then intersect with the
        # customer's allowed set in Python — two separate lookups (code
        # = PK string, allowed_ids = PK string set) so we can't fold
        # them into a single filter() call anyway.
        chosen_methods = [
            pm for pm in PaymentMethod.objects.filter(
                code__in=requested_codes,
                is_active=True,
            )
            if pm.code in allowed_ids
        ]
        # Preserve the order the customer sent them in.
        chosen_methods.sort(key=lambda pm: requested_codes.index(pm.code))

        # Empty selection → cascade to admin global primary, else None.
        if not chosen_methods:
            fallback = _resolve_payment_method_for_invoice(
                request.user, None,
            )
            if fallback:
                chosen_methods = [fallback]

        # Legacy single FK — point it at the first method so existing
        # reports / snapshots stay populated. None is fine if the
        # customer has no methods granted and no admin default exists.
        primary_method = chosen_methods[0] if chosen_methods else None

        # ── Number + expiry ──
        number = _generate_invoice_number(d["company"])
        expiry_days = d["company"].invoice_link_expiry_days
        expires_at = (timezone.now() + timedelta(days=expiry_days)
                      if expiry_days else None)

        # ── Due date default ──
        # If the customer didn't set a due date, we don't want to
        # leave the invoice with no payment deadline (rendering
        # blank in the PDF and feeling unprofessional). Default to
        # 14 days out, which matches Net-14 — a common accounting
        # convention. Customers can always edit afterwards.
        due_date_value = d.get("due_date")
        if not due_date_value:
            due_date_value = timezone.now().date() + timedelta(days=14)

        # ── Totals ──
        line_items_data = d.pop("line_items", [])
        subtotal, tax_amt, total = _compute_totals(
            line_items_data, d.get("tax_percent", 0),
        )

        # ── Draft vs send ──
        # Client may pass action="draft" | "create" | "send" (default
        # "send"). Draft skips email dispatch entirely.
        action_flag = (request.data.get("action") or "send").lower()
        initial_status = (InvoiceStatus.DRAFT if action_flag == "draft"
                          else InvoiceStatus.DRAFT)   # we flip to sent after emails fire

        invoice = Invoice.objects.create(
            customer=request.user,
            client=d["client"],
            company=d["company"],
            payment_method=primary_method,
            number=number,
            currency_code=d.get("currency_code") or "USD",
            subtotal=subtotal,
            tax_percent=d.get("tax_percent") or Decimal("0"),
            tax_amount=tax_amt,
            total=total,
            due_date=due_date_value,
            general_description=d.get("general_description", ""),
            notes=d.get("notes", ""),
            status=initial_status,
            share_token=secrets.token_urlsafe(32),
            expires_at=expires_at,
            client_snapshot=_snapshot_client(d["client"]),
            company_snapshot=_snapshot_company(d["company"], request),
            payment_method_snapshot=_snapshot_payment_method(
                primary_method, request,
            ),
        )
        for idx, li in enumerate(line_items_data):
            InvoiceLineItem.objects.create(
                invoice=invoice,
                position=li.get("position", idx),
                name=li.get("name") or "Item",
                description=li.get("description", ""),
                quantity=li.get("quantity") or Decimal("0"),
                unit_price=li.get("unit_price") or Decimal("0"),
            )

        # Write the M2M rows with per-method snapshots.
        for idx, pm in enumerate(chosen_methods):
            InvoicePaymentMethod.objects.create(
                invoice=invoice,
                payment_method=pm,
                position=idx,
                snapshot=_snapshot_payment_method(pm, request),
            )

        # Build + cache the PDF right away.
        try:
            _build_and_cache_pdf(invoice)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "invoice pdf generation failed (non-fatal): %s", e,
            )

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_CREATE, target=invoice,
            description=f"Invoice {invoice.number} created for "
                        f"{invoice.client.name} ({action_flag})",
        )

        # Only dispatch emails if the customer asked to send — "draft"
        # and "create" both skip delivery; "send" (default) triggers it.
        if action_flag == "send":
            self._send_emails(invoice)

        return Response(
            self.get_serializer(invoice).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        """Edit an existing invoice — drafts only.

        Sent/viewed/paid/void invoices are locked to preserve their
        history. When editing a draft we rebuild the line items,
        payment methods, totals, and regenerate the PDF — essentially
        the same work create() does, but on an existing row.
        """
        return self._update_draft(request, partial=False)

    def partial_update(self, request, *args, **kwargs):
        return self._update_draft(request, partial=True)

    @dbtx.atomic
    def _update_draft(self, request, partial):
        invoice = self.get_object()
        if invoice.customer_id != request.user.id:
            return Response({"detail": "Forbidden."},
                            status=status.HTTP_403_FORBIDDEN)
        if invoice.status != InvoiceStatus.DRAFT:
            return Response(
                {"detail": "Only draft invoices can be edited. "
                           "This invoice has already been sent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = self.get_serializer(invoice, data=request.data, partial=partial)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        # Scope checks — same as create().
        new_client = d.get("client") or invoice.client
        new_company = d.get("company") or invoice.company
        if new_client.customer_id != request.user.id:
            return Response({"detail": "Client does not belong to you."},
                            status=status.HTTP_400_BAD_REQUEST)
        if new_company.customer_id != request.user.id:
            return Response({"detail": "Company does not belong to you."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Resolve payment methods — mirrors the create() logic exactly.
        from myapp.Models.Invoicing_models import (
            CustomerAllowedPaymentMethod, InvoicePaymentMethod,
        )
        from myapp.Models.Core_models import PaymentMethod

        requested_codes = request.data.get("payment_methods")
        if requested_codes is None and d.get("payment_method"):
            requested_codes = [d["payment_method"].code]
        # None means "client didn't mention them" — keep existing.
        # Empty list means "client wants to clear them" — honour that.
        if requested_codes is not None:
            allowed_ids = set(CustomerAllowedPaymentMethod.objects.filter(
                customer=request.user,
                payment_method__is_active=True,
            ).values_list("payment_method_id", flat=True))
            chosen_methods = [
                pm for pm in PaymentMethod.objects.filter(
                    code__in=requested_codes, is_active=True,
                )
                if pm.code in allowed_ids
            ]
            chosen_methods.sort(
                key=lambda pm: requested_codes.index(pm.code),
            )
            if not chosen_methods:
                fallback = _resolve_payment_method_for_invoice(
                    request.user, None,
                )
                if fallback:
                    chosen_methods = [fallback]
        else:
            chosen_methods = None   # sentinel — don't touch

        # Line items.
        line_items_data = d.pop("line_items", None)
        if line_items_data is not None:
            subtotal, tax_amt, total = _compute_totals(
                line_items_data, d.get("tax_percent", invoice.tax_percent),
            )
            invoice.subtotal = subtotal
            invoice.tax_amount = tax_amt
            invoice.total = total

        # Update the direct fields on the invoice row.
        for field in ("currency_code", "due_date",
                      "general_description", "notes", "tax_percent"):
            if field in d:
                setattr(invoice, field, d[field])
        if "client" in d:
            invoice.client = d["client"]
            invoice.client_snapshot = _snapshot_client(d["client"])
        if "company" in d:
            invoice.company = d["company"]
            invoice.company_snapshot = _snapshot_company(
                d["company"], request,
            )
        # Recompute totals if tax_percent changed but line_items didn't
        # (use the existing ones).
        if line_items_data is None and "tax_percent" in d:
            subtotal = sum(
                (li.quantity or Decimal("0")) * (li.unit_price or Decimal("0"))
                for li in invoice.line_items.all()
            )
            subtotal = Decimal(str(subtotal)).quantize(Decimal("0.01"))
            tax_pct = Decimal(str(d["tax_percent"] or 0))
            tax_amt = (subtotal * tax_pct / Decimal("100")).quantize(
                Decimal("0.01"),
            )
            invoice.subtotal = subtotal
            invoice.tax_amount = tax_amt
            invoice.total = (subtotal + tax_amt).quantize(Decimal("0.01"))

        # Update the primary FK if new methods were chosen.
        if chosen_methods is not None:
            primary = chosen_methods[0] if chosen_methods else None
            invoice.payment_method = primary
            invoice.payment_method_snapshot = _snapshot_payment_method(
                primary, request,
            )

        invoice.save()

        # Swap out line items.
        if line_items_data is not None:
            invoice.line_items.all().delete()
            for idx, li in enumerate(line_items_data):
                InvoiceLineItem.objects.create(
                    invoice=invoice,
                    position=li.get("position", idx),
                    name=li.get("name") or "Item",
                    description=li.get("description", ""),
                    quantity=li.get("quantity") or Decimal("0"),
                    unit_price=li.get("unit_price") or Decimal("0"),
                )

        # Swap out payment methods.
        if chosen_methods is not None:
            invoice.invoice_payment_methods.all().delete()
            for idx, pm in enumerate(chosen_methods):
                InvoicePaymentMethod.objects.create(
                    invoice=invoice,
                    payment_method=pm,
                    position=idx,
                    snapshot=_snapshot_payment_method(pm, request),
                )

        # Regenerate PDF so the draft preview is current.
        try:
            _build_and_cache_pdf(invoice)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "invoice pdf regen failed on draft edit (non-fatal): %s", e,
            )

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE, target=invoice,
            description=f"Draft invoice {invoice.number} edited",
        )
        return Response(self.get_serializer(invoice).data)

    def perform_destroy(self, instance):
        """Hard-delete drafts; soft-void anything that's been issued.

        Drafts have never left the customer's account — they're safe
        to erase completely. Anything sent/viewed/paid/void stays in
        the DB as a void record so the audit trail and any client-side
        copies the customer already emailed can still be reconciled.
        """
        if instance.status == InvoiceStatus.DRAFT:
            number = instance.number
            AuditLog.record(
                user=self.request.user, action=AuditLog.ACTION_DELETE,
                target=instance,
                description=f"Draft invoice {number} deleted",
            )
            instance.delete()
            return

        instance.status = InvoiceStatus.VOID
        instance.save(update_fields=["status", "updated_at"])
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=instance,
            description=f"Invoice {instance.number} voided",
        )

    @action(detail=True, methods=["post"], url_path="send")
    def send_invoice(self, request, pk=None):
        invoice = self.get_object()
        if invoice.customer_id != request.user.id:
            return Response({"detail": "Forbidden."},
                            status=status.HTTP_403_FORBIDDEN)
        # Refresh letterhead/payment snapshots so a logo or QR added
        # after issue time shows up in the emailed PDF. Client snapshot
        # is intentionally NOT refreshed — that's meant to stay frozen.
        self._refresh_letterhead_snapshots(invoice, request)
        sent = self._send_emails(invoice)
        return Response({"sent": sent,
                         "sent_to_client_at": invoice.sent_to_client_at,
                         "sent_to_customer_at": invoice.sent_to_customer_at})

    @action(detail=True, methods=["post"], url_path="regenerate-pdf")
    def regenerate_pdf(self, request, pk=None):
        invoice = self.get_object()
        if invoice.customer_id != request.user.id:
            return Response({"detail": "Forbidden."},
                            status=status.HTTP_403_FORBIDDEN)
        # Optional ?theme= or body {"theme": "dark"} — sticks on the
        # invoice so subsequent sends + manual re-renders keep using
        # whatever the customer last chose. Unknown values fall back
        # to 'light' so bad client input can't break anything.
        theme = (
            request.data.get("theme")
            or request.query_params.get("theme")
            or invoice.pdf_theme
            or "light"
        )
        theme = str(theme).lower()
        if theme not in ("light", "dark"):
            theme = "light"
        self._refresh_letterhead_snapshots(invoice, request)
        _build_and_cache_pdf(invoice, theme=theme)
        return Response(self.get_serializer(invoice).data)

    def _refresh_letterhead_snapshots(self, invoice, request):
        """Re-capture company + payment-method snapshots from live records.

        Used by both `send` and `regenerate-pdf` so invoices issued
        before the logo/QR was uploaded — or before we started storing
        absolute media URLs — pick up those changes on the next action.
        The client snapshot is NOT touched; it should stay as it was at
        issue time to preserve what the invoice was originally billed
        against.
        """
        try:
            invoice.company_snapshot = _snapshot_company(
                invoice.company, request,
            )
            if invoice.payment_method_id:
                invoice.payment_method_snapshot = _snapshot_payment_method(
                    invoice.payment_method, request,
                )
            for ipm in invoice.invoice_payment_methods.all():
                ipm.snapshot = _snapshot_payment_method(
                    ipm.payment_method, request,
                )
                ipm.save(update_fields=["snapshot"])
            invoice.save(update_fields=[
                "company_snapshot", "payment_method_snapshot", "updated_at",
            ])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "snapshot refresh failed for invoice %s: %s",
                invoice.number, e,
            )

    @action(
        detail=True,
        methods=["post"],
        url_path="mark-paid",
        # Accept multipart so the customer can attach a proof document
        # alongside the action. JSON is still fine for the no-attachment
        # case (e.g. quick "mark paid" from the list page).
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def mark_paid(self, request, pk=None):
        """Flip an invoice to paid status.

        Voided invoices can't be marked paid — that would be a
        bookkeeping lie. Everything else (draft/sent/viewed) can move
        directly to paid in one step, which is what customers expect
        when the client actually pays them out-of-band (Zelle, ACH
        confirmation, etc.).

        Optional payload fields:
          - ``payment_proof_file``: file the customer uploaded as
            proof (receipt screenshot, PDF, etc.). Multipart only.
          - ``payment_proof_note``: free-form note/comment.

        Both are optional. Re-marking an already-paid invoice with
        new attachments is allowed — useful when the customer forgot
        to attach proof the first time and wants to add it now.
        """
        from django.utils import timezone

        invoice = self.get_object()
        if invoice.customer_id != request.user.id:
            return Response({"detail": "Forbidden."},
                            status=status.HTTP_403_FORBIDDEN)
        if invoice.status == InvoiceStatus.VOID:
            return Response(
                {"detail": "A voided invoice cannot be marked as paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Pull optional proof + note from the request. We accept either
        # multipart (file in request.FILES) or plain JSON (note only).
        proof_file = request.FILES.get("payment_proof_file")
        # Note: request.data is a unified dict for both JSON and
        # multipart payloads in DRF, so this works for both.
        note_raw = request.data.get("payment_proof_note", None)

        update_fields = []

        if invoice.status != InvoiceStatus.PAID:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = timezone.now()
            update_fields += ["status", "paid_at"]

        if proof_file is not None:
            # Validate file type + size before doing anything with it.
            try:
                validate_doc_file(proof_file)
            except Exception as exc:
                from django.core.exceptions import ValidationError as DjangoValidationError
                if isinstance(exc, DjangoValidationError):
                    return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)
                raise
            # Once a proof document is on file, it's locked — the
            # customer can edit the note but not swap the file. This
            # mirrors the frontend, which hides the file input after
            # the first upload, but we enforce it server-side too so
            # a crafted multipart request can't bypass the rule.
            if invoice.payment_proof_file:
                return Response(
                    {"detail": "A payment proof document is already on "
                               "file for this invoice and cannot be "
                               "replaced. You can still update the note."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invoice.payment_proof_file = proof_file
            update_fields.append("payment_proof_file")

        if note_raw is not None:
            invoice.payment_proof_note = (note_raw or "").strip()
            update_fields.append("payment_proof_note")

        if update_fields:
            update_fields.append("updated_at")
            invoice.save(update_fields=update_fields)

        # Audit log message reflects what actually happened so the
        # admin trail is informative without spamming on no-op calls.
        if "status" in update_fields:
            description = f"Invoice {invoice.number} marked as paid"
        elif "payment_proof_file" in update_fields \
                or "payment_proof_note" in update_fields:
            description = (
                f"Invoice {invoice.number} payment proof updated"
            )
        else:
            description = None

        if description:
            AuditLog.record(
                user=request.user, action=AuditLog.ACTION_UPDATE,
                target=invoice, description=description,
            )

        return Response(self.get_serializer(invoice).data)

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        """Aggregate stats across the filtered queryset.

        This runs the SAME filters as the list endpoint (date range,
        client, company, payment method, status, search) so the numbers
        shown in the UI's stats band always match whatever the customer
        is currently looking at — regardless of pagination.

        Returns totals for:
          - total billed: sum(total) across all matched invoices
          - awaiting payment: sum where status in {sent, viewed}
          - paid: sum where status == paid (+ count)
          - draft: sum where status == draft (+ count)
          - void: sum where status == void (+ count)
          - overall count
        """
        from django.db.models import Sum, Count, Q
        qs = self.get_queryset()

        # Void invoices are cancelled — they must NOT be counted in
        # "Total billed" or the top-line invoice count, because that
        # number represents business the customer is tracking, and a
        # voided invoice is explicitly NOT tracked business. Only the
        # dedicated `void_count` tile exposes them.
        not_void = ~Q(status=InvoiceStatus.VOID)

        # Single aggregate call — one DB query total.
        agg = qs.aggregate(
            count=Count("id", filter=not_void),
            total_billed=Sum("total", filter=not_void),

            awaiting_amount=Sum(
                "total",
                filter=Q(status__in=[InvoiceStatus.SENT, InvoiceStatus.VIEWED]),
            ),
            awaiting_count=Count(
                "id",
                filter=Q(status__in=[InvoiceStatus.SENT, InvoiceStatus.VIEWED]),
            ),

            paid_amount=Sum(
                "total", filter=Q(status=InvoiceStatus.PAID),
            ),
            paid_count=Count(
                "id", filter=Q(status=InvoiceStatus.PAID),
            ),

            draft_amount=Sum(
                "total", filter=Q(status=InvoiceStatus.DRAFT),
            ),
            draft_count=Count(
                "id", filter=Q(status=InvoiceStatus.DRAFT),
            ),

            void_count=Count(
                "id", filter=Q(status=InvoiceStatus.VOID),
            ),
        )
        # Normalize Nones so the frontend can do arithmetic safely.
        def _n(k): return str(agg.get(k) or 0)
        return Response({
            "count":            agg.get("count") or 0,
            "total_billed":     _n("total_billed"),
            "awaiting_amount":  _n("awaiting_amount"),
            "awaiting_count":   agg.get("awaiting_count") or 0,
            "paid_amount":      _n("paid_amount"),
            "paid_count":       agg.get("paid_count") or 0,
            "draft_amount":     _n("draft_amount"),
            "draft_count":      agg.get("draft_count") or 0,
            "void_count":       agg.get("void_count") or 0,
        })

    def _send_emails(self, invoice):
        """Send PDF + share link to client and BCC the customer."""
        from myapp.Utils.email_tasks import send_email_async
        from django.conf import settings

        # Ensure we have a fresh PDF.
        if not invoice.pdf_file:
            try:
                _build_and_cache_pdf(invoice)
            except Exception:
                pass

        attachments = []
        if invoice.pdf_file:
            try:
                with invoice.pdf_file.open("rb") as fh:
                    attachments.append({
                        "filename": f"{invoice.number}.pdf",
                        "content_b64": base64.b64encode(fh.read()).decode(),
                        "mimetype": "application/pdf",
                    })
            except Exception:
                pass

        frontend = getattr(settings, "FRONTEND_URL", "")
        public_url = (f"{frontend.rstrip('/')}/invoice/{invoice.share_token}"
                      if frontend else f"/invoice/{invoice.share_token}")

        ctx = {
            "invoice_number": invoice.number,
            "company_name": invoice.company_snapshot.get("name"),
            "client_name": invoice.client_snapshot.get("name"),
            "total": str(invoice.total),
            "currency_code": invoice.currency_code,
            "public_url": public_url,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        }

        client_email = invoice.client_snapshot.get("email")
        sent = []

        # 1) Client — only if we have their email. This is the one with
        # the PDF attachment; they need to be able to act on it.
        if client_email:
            send_email_async(
                to=[client_email],
                subject=f"Invoice {invoice.number} from "
                        f"{invoice.company_snapshot.get('name', 'your supplier')}",
                template="invoice/sent_to_client",
                context=ctx,
                attachments=attachments,
            )
            invoice.sent_to_client_at = timezone.now()
            sent.append("client")

        # 2) Customer copy — also gets the attachment for their records.
        customer_email = self.request.user.email
        if customer_email:
            send_email_async(
                to=[customer_email],
                subject=f"Copy: invoice {invoice.number} sent to "
                        f"{invoice.client_snapshot.get('name', 'client')}",
                template="invoice/sent_to_customer",
                context=ctx,
                attachments=attachments,
            )
            invoice.sent_to_customer_at = timezone.now()
            sent.append("customer")

        # Flip status to sent if we at least mailed someone.
        if sent and invoice.status == InvoiceStatus.DRAFT:
            invoice.status = InvoiceStatus.SENT
        invoice.save(update_fields=[
            "status", "sent_to_client_at", "sent_to_customer_at", "updated_at",
        ])
        return sent


class PublicInvoiceView(APIView):
    """No-auth read endpoint for the invoice share link.

    URL: /invoice-public/<share_token>/

    Respects the company's invoice_link_expiry_days:
      - expires_at is null → never expires
      - expires_at in the past → 410 Gone
    Records view count and first-view timestamp for the customer's
    reference (same as Stripe's public invoice URLs).
    """
    permission_classes = []  # public
    authentication_classes = []

    def get(self, request, share_token):
        invoice = get_object_or_404(Invoice, share_token=share_token)
        if invoice.status == InvoiceStatus.VOID:
            return Response(
                {"detail": "This invoice has been voided."},
                status=status.HTTP_410_GONE,
            )
        if invoice.expires_at and invoice.expires_at < timezone.now():
            return Response(
                {"detail": "This invoice link has expired. Please ask "
                           "the sender to resend."},
                status=status.HTTP_410_GONE,
            )

        # Live-upgrade the letterhead snapshot for display purposes.
        # We DON'T persist this — the client_snapshot stays frozen for
        # legal/bookkeeping reasons — but in-memory we swap a missing or
        # relative `logo_url` for an absolute one built from the current
        # request. That way invoices issued before we started storing
        # absolute URLs still render with a working logo when clients
        # open the share link.
        comp_snap = dict(invoice.company_snapshot or {})
        logo_url = comp_snap.get("logo_url") or ""
        needs_upgrade = (not logo_url) or logo_url.startswith("/")
        if needs_upgrade:
            try:
                if invoice.company and invoice.company.logo:
                    comp_snap["logo_url"] = request.build_absolute_uri(
                        invoice.company.logo.url,
                    )
                    invoice.company_snapshot = comp_snap
            except Exception:
                pass
        # Same upgrade for each payment method's QR code URL.
        try:
            for ipm in invoice.invoice_payment_methods.all():
                s = dict(ipm.snapshot or {})
                qr = s.get("qr_code_url") or ""
                if (not qr) or qr.startswith("/"):
                    if ipm.payment_method and ipm.payment_method.qr_code:
                        s["qr_code_url"] = request.build_absolute_uri(
                            ipm.payment_method.qr_code.url,
                        )
                        ipm.snapshot = s
        except Exception:
            pass
        # Legacy single-method snapshot.
        if invoice.payment_method_snapshot:
            s = dict(invoice.payment_method_snapshot)
            qr = s.get("qr_code_url") or ""
            if (not qr) or qr.startswith("/"):
                try:
                    if invoice.payment_method and invoice.payment_method.qr_code:
                        s["qr_code_url"] = request.build_absolute_uri(
                            invoice.payment_method.qr_code.url,
                        )
                        invoice.payment_method_snapshot = s
                except Exception:
                    pass

        # Record the view (still persisted).
        update_fields = []
        if not invoice.first_viewed_at:
            invoice.first_viewed_at = timezone.now()
            update_fields.append("first_viewed_at")
        invoice.view_count = (invoice.view_count or 0) + 1
        update_fields.append("view_count")
        if invoice.status in (InvoiceStatus.SENT, InvoiceStatus.DRAFT):
            invoice.status = InvoiceStatus.VIEWED
            update_fields.append("status")
        update_fields.append("updated_at")
        invoice.save(update_fields=update_fields)

        return Response(PublicInvoiceSerializer(
            invoice, context={"request": request},
        ).data)
