"""Serializers for the invoicing module (clients + customer companies)."""
from rest_framework import serializers

from myapp.Models.Invoicing_models import Client, CustomerCompany


def _cloudinary_attachment_url(url, filename):
    """Inject Cloudinary's fl_attachment flag so the browser downloads
    the file instead of opening it inline.

    Cloudinary serves uploaded files with URLs that have no ``.pdf``
    extension and no ``Content-Disposition: attachment`` header, so
    a plain ``<a href>`` click just opens the PDF in a new tab. The
    ``fl_attachment:<filename>`` flag tells Cloudinary to send the
    response as an attachment with that filename.

    Non-Cloudinary URLs (local dev, custom storage) are returned
    unchanged. URLs that already have the flag are also left alone.
    """
    if not url:
        return url
    if "res.cloudinary.com" not in url:
        return url
    if "/upload/" not in url:
        return url
    if "fl_attachment" in url:
        return url
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(filename or "invoice"))
    return url.replace("/upload/", f"/upload/fl_attachment:{safe}/", 1)


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = [
            "id", "name", "company_name",
            "email", "phone", "address",
            "notes",
            "is_archived",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CustomerCompanySerializer(serializers.ModelSerializer):
    # Read-only URL for the logo; writes accept a plain file upload via the
    # default multipart parser on the viewset.
    logo_url = serializers.SerializerMethodField()

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        req = self.context.get("request")
        try:
            return req.build_absolute_uri(obj.logo.url) if req else obj.logo.url
        except Exception:
            return None

    class Meta:
        model = CustomerCompany
        fields = [
            "id", "name", "email", "phone", "website",
            "address_line1", "address_line2",
            "city", "state", "postal_code", "country",
            "tax_id",
            "logo", "logo_url",
            "is_primary",
            "invoice_link_expiry_days",
            "invoice_number_prefix", "next_invoice_number",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "created_at", "updated_at",
            "next_invoice_number",   # server increments on invoice creation
            "logo_url",
        ]


# ──────────────────────────────────────────────────────────────────────
# Admin-facing Payment Method configuration
# ──────────────────────────────────────────────────────────────────────

from myapp.Models.Core_models import PaymentMethod
from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod


class PaymentMethodConfigSerializer(serializers.ModelSerializer):
    """Full admin-editable view of a PaymentMethod row. Staff only."""
    qr_code_url = serializers.SerializerMethodField()

    def get_qr_code_url(self, obj):
        if not obj.qr_code:
            return None
        req = self.context.get("request")
        try:
            return req.build_absolute_uri(obj.qr_code.url) if req else obj.qr_code.url
        except Exception:
            return None

    class Meta:
        model = PaymentMethod
        fields = [
            "code", "label", "is_active", "sort_order",
            "email", "phone", "cashapp_tag",
            "holder_name",
            "account_number", "routing_number",
            "bank_name", "account_type",
            "address_line1", "address_line2",
            "city", "state", "postal_code", "country",
            "qr_code", "qr_code_url",
            "instructions",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at", "qr_code_url"]


class CustomerAllowedPaymentMethodSerializer(serializers.ModelSerializer):
    """Row in the per-customer allowed-methods matrix."""
    payment_method_label = serializers.CharField(
        source="payment_method.label", read_only=True,
    )
    granted_by_email = serializers.EmailField(
        source="granted_by.email", read_only=True,
    )

    class Meta:
        model = CustomerAllowedPaymentMethod
        fields = [
            "id", "customer", "payment_method",
            "payment_method_label",
            "is_primary",
            "granted_at", "granted_by_email",
        ]
        read_only_fields = ["id", "granted_at", "granted_by_email",
                            "payment_method_label"]


# ──────────────────────────────────────────────────────────────────────
#  Invoices
# ──────────────────────────────────────────────────────────────────────

from decimal import Decimal
from myapp.Models.Invoicing_models import Invoice, InvoiceLineItem


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    total = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True,
    )

    class Meta:
        model = InvoiceLineItem
        fields = [
            "id", "position", "name", "description",
            "quantity", "unit_price", "total",
        ]
        read_only_fields = ["id", "total"]


class InvoiceSerializer(serializers.ModelSerializer):
    """Full-fat invoice serializer — used for list, detail, and create.

    On create, accepts nested `line_items` and computes subtotal/tax/total
    server-side so the client can't fudge numbers. Client/company/method
    are all IDs; the server snapshots their details into the invoice
    record at create time.
    """
    line_items = InvoiceLineItemSerializer(many=True)
    public_url = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()
    pdf_download_url = serializers.SerializerMethodField()
    # Multi-method selection. Read-only on the main serializer — writes
    # happen via the `payment_methods` (plural) list of codes on the
    # create payload, handled in the viewset.
    invoice_payment_methods = serializers.SerializerMethodField()

    def get_invoice_payment_methods(self, obj):
        return [
            {
                "code": ipm.payment_method_id,
                "label": (ipm.snapshot or {}).get("label")
                         or ipm.payment_method.label,
                "snapshot": ipm.snapshot or {},
                "position": ipm.position,
            }
            for ipm in obj.invoice_payment_methods.all().order_by(
                "position", "id",
            )
        ]

    # Convenience read-only fields so the list view has labels without
    # the frontend having to join.
    client_name = serializers.CharField(source="client.name", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    payment_method_label = serializers.SerializerMethodField()

    def get_public_url(self, obj):
        req = self.context.get("request")
        path = f"/invoice/{obj.share_token}"
        if req:
            # Prefer the configured frontend URL (for emails / sharing)
            # but fall back to same-host + path.
            from django.conf import settings
            base = getattr(settings, "FRONTEND_URL", "") or ""
            if base:
                return f"{base.rstrip('/')}{path}"
            return req.build_absolute_uri(path)
        return path

    def get_pdf_url(self, obj):
        """Return the raw PDF URL (no fl_attachment flag).

        Used for INLINE preview (<object>, <iframe>). For download
        buttons, use `pdf_download_url` which forces the browser to
        save the file via Cloudinary's fl_attachment flag.
        """
        if not obj.pdf_file:
            return None
        req = self.context.get("request")
        try:
            raw = obj.pdf_file.url
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    def get_pdf_download_url(self, obj):
        """Return a download-forcing PDF URL.

        Cloudinary-hosted PDFs get `fl_attachment:<number>` injected
        so the response comes back with `Content-Disposition:
        attachment` — the browser saves instead of previewing.
        """
        if not obj.pdf_file:
            return None
        req = self.context.get("request")
        try:
            raw = obj.pdf_file.url
            raw = _cloudinary_attachment_url(raw, obj.number)
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    def get_payment_method_label(self, obj):
        if obj.payment_method_id:
            return (obj.payment_method_snapshot or {}).get("label") \
                or obj.payment_method.label
        return None

    class Meta:
        model = Invoice
        fields = [
            "id", "number",
            "customer",
            "client", "client_name",
            "company", "company_name",
            "payment_method", "payment_method_label",
            "invoice_payment_methods",
            "currency_code",
            "line_items",
            "subtotal", "tax_percent", "tax_amount", "total",
            "issue_date", "due_date",
            "general_description", "notes",
            "status",
            "share_token", "public_url", "pdf_url", "pdf_download_url",
            "expires_at", "first_viewed_at", "view_count",
            "sent_to_client_at", "sent_to_customer_at",
            "client_snapshot", "company_snapshot", "payment_method_snapshot",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "number",
            "customer",
            "subtotal", "tax_amount", "total",
            "issue_date",
            "share_token", "public_url", "pdf_url", "pdf_download_url",
            "expires_at", "first_viewed_at", "view_count",
            "sent_to_client_at", "sent_to_customer_at",
            "client_snapshot", "company_snapshot", "payment_method_snapshot",
            "status",
            "created_at", "updated_at",
            "client_name", "company_name", "payment_method_label",
            "invoice_payment_methods",
        ]


class PublicInvoiceSerializer(serializers.ModelSerializer):
    """Read-only serializer for the public share-link view.

    Intentionally omits fields that could leak customer/admin data:
    - no customer FK (we don't expose our user's ID to the world)
    - no raw `pdf_file` storage path — callers get `pdf_url` instead
    - no expiry/view-count internals
    """
    line_items = InvoiceLineItemSerializer(many=True, read_only=True)
    pdf_url = serializers.SerializerMethodField()
    pdf_download_url = serializers.SerializerMethodField()
    invoice_payment_methods = serializers.SerializerMethodField()

    def get_invoice_payment_methods(self, obj):
        return [
            {
                "code": ipm.payment_method_id,
                "label": (ipm.snapshot or {}).get("label")
                         or ipm.payment_method.label,
                "snapshot": ipm.snapshot or {},
                "position": ipm.position,
            }
            for ipm in obj.invoice_payment_methods.all().order_by(
                "position", "id",
            )
        ]

    def get_pdf_url(self, obj):
        """Raw inline URL (no fl_attachment) — used for <object> preview."""
        if not obj.pdf_file:
            return None
        req = self.context.get("request")
        try:
            raw = obj.pdf_file.url
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    def get_pdf_download_url(self, obj):
        """Download-forcing URL with Cloudinary fl_attachment flag."""
        if not obj.pdf_file:
            return None
        req = self.context.get("request")
        try:
            raw = obj.pdf_file.url
            raw = _cloudinary_attachment_url(raw, obj.number)
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    class Meta:
        model = Invoice
        fields = [
            "number", "currency_code",
            "issue_date", "due_date",
            "subtotal", "tax_percent", "tax_amount", "total",
            "general_description", "notes",
            "client_snapshot", "company_snapshot", "payment_method_snapshot",
            "invoice_payment_methods",
            "line_items",
            "pdf_url",
            "pdf_download_url",
        ]
