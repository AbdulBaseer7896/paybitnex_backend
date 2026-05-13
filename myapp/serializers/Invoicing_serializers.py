"""Serializers for the invoicing module (clients + customer companies)."""
import logging
import re

from rest_framework import serializers

from myapp.Models.Invoicing_models import Client, CustomerCompany

log = logging.getLogger(__name__)


def _signed_s3_download_url(fieldfile, filename, ttl=None):
    """Return a pre-signed S3 URL that forces the browser to save
    the file as ``<filename>.pdf`` via Content-Disposition.

    Works only when the storage backend is django-storages' S3Storage.
    Returns ``None`` for any other backend so callers know to fall
    back to the Cloudinary helper or the plain ``.url``.

    ``ttl`` overrides ``settings.AWS_QUERYSTRING_EXPIRE``. Useful for
    the public invoice page which wants the URL to expire when the
    share link itself expires.
    """
    if not fieldfile:
        return None
    try:
        from django.conf import settings as dj_settings
        storage = fieldfile.storage
        # Duck-type: only S3Storage exposes `.connection`.
        if not hasattr(storage, "connection"):
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_",
                      str(filename or "download"))

        # django-storages stores the key as-is under AWS_LOCATION
        # (if the backend was configured with AWS_LOCATION). Use
        # `storage.bucket_name` + `storage._normalize_name` to get
        # the real object key — this is what django-storages uses
        # internally when resolving a URL.
        key = storage._normalize_name(storage._clean_name(fieldfile.name))

        return storage.connection.meta.client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": storage.bucket_name,
                "Key": key,
                "ResponseContentDisposition":
                    f'attachment; filename="{safe}.pdf"',
                "ResponseContentType": "application/pdf",
            },
            ExpiresIn=int(ttl or dj_settings.AWS_QUERYSTRING_EXPIRE),
        )
    except Exception as e:
        log.warning("signed S3 download URL generation failed: %s", e)
        return None


def _cloudinary_attachment_url(url, filename):
    """Legacy Cloudinary helper — kept for invoices whose PDFs were
    uploaded to Cloudinary BEFORE the S3 migration. New uploads go
    to S3 and use _signed_s3_download_url() above.
    """
    if not url:
        return url
    if "res.cloudinary.com" not in url:
        return url
    if "/upload/" not in url:
        return url
    if "fl_attachment" in url:
        return url
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
            "code", "label", "is_active", "is_default", "sort_order",
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
    # Payment-receipt proof — pre-signed S3 URLs the frontend can use
    # to render or download the document the customer attached when
    # marking the invoice as paid. Empty when no proof was uploaded.
    payment_proof_url = serializers.SerializerMethodField()
    payment_proof_download_url = serializers.SerializerMethodField()
    payment_proof_filename = serializers.SerializerMethodField()
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

        New S3 PDFs: generate a short-lived pre-signed URL with
        ``Content-Disposition: attachment`` baked into the signed
        params. The URL itself is the HTTPS S3 endpoint, bounded by
        ``settings.AWS_QUERYSTRING_EXPIRE``.

        Legacy Cloudinary PDFs (from before the S3 migration): fall
        back to the ``fl_attachment:<number>`` URL trick.
        """
        if not obj.pdf_file:
            return None
        # 1) Try S3 first — the common case going forward.
        s3_url = _signed_s3_download_url(obj.pdf_file, obj.number)
        if s3_url:
            return s3_url
        # 2) Fall back to the legacy Cloudinary flag. This path
        #    only fires for invoices whose pdf_file was uploaded
        #    before the S3 switchover.
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

    # ── Payment-proof helpers ─────────────────────────────────────
    # We expose two URLs for symmetry with the invoice PDF: a plain
    # pre-signed URL for inline preview (image / PDF in <object>) and
    # a download-forcing URL for the explicit "Download" button.
    # Both fall back to the field's raw .url for non-S3 backends so
    # local development without S3 still works.
    def _proof_basename(self, obj):
        if not obj.payment_proof_file:
            return None
        try:
            import os
            return os.path.basename(obj.payment_proof_file.name)
        except Exception:
            return None

    def get_payment_proof_filename(self, obj):
        return self._proof_basename(obj)

    def get_payment_proof_url(self, obj):
        """Inline preview URL for the proof document.

        Uses a plain pre-signed URL on S3 so images render and PDFs
        open inline in the browser. Falls back to the raw .url for
        local/legacy storage.
        """
        if not obj.payment_proof_file:
            return None
        storage = obj.payment_proof_file.storage
        # Duck-type S3 backend: it exposes .connection, .bucket_name.
        if hasattr(storage, "connection"):
            try:
                from django.conf import settings as dj_settings
                key = storage._normalize_name(
                    storage._clean_name(obj.payment_proof_file.name),
                )
                return storage.connection.meta.client.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={
                        "Bucket": storage.bucket_name,
                        "Key": key,
                    },
                    ExpiresIn=int(dj_settings.AWS_QUERYSTRING_EXPIRE),
                )
            except Exception as e:
                log.warning("payment proof inline URL failed: %s", e)
        # Non-S3 fallback (local dev / Cloudinary).
        req = self.context.get("request")
        try:
            raw = obj.payment_proof_file.url
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    def get_payment_proof_download_url(self, obj):
        """Download-forcing URL for the proof document."""
        if not obj.payment_proof_file:
            return None
        # Reuse the S3 helper but pass the *original* filename so the
        # downloaded file keeps its extension (the helper appends .pdf
        # by default — we want to preserve whatever the customer
        # uploaded, e.g. .png / .jpg / .pdf).
        try:
            from django.conf import settings as dj_settings
            storage = obj.payment_proof_file.storage
            if hasattr(storage, "connection"):
                import os
                base = os.path.basename(obj.payment_proof_file.name)
                # Sanitise for Content-Disposition.
                safe = re.sub(r'[\\/:*?"<>|]+', "_", base)
                key = storage._normalize_name(
                    storage._clean_name(obj.payment_proof_file.name),
                )
                return storage.connection.meta.client.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={
                        "Bucket": storage.bucket_name,
                        "Key": key,
                        "ResponseContentDisposition":
                            f'attachment; filename="{safe}"',
                    },
                    ExpiresIn=int(dj_settings.AWS_QUERYSTRING_EXPIRE),
                )
        except Exception as e:
            log.warning("payment proof download URL failed: %s", e)
        # Non-S3 fallback — same as inline URL; browser will decide.
        return self.get_payment_proof_url(obj)

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
            "pdf_theme",
            "expires_at", "first_viewed_at", "view_count",
            "sent_to_client_at", "sent_to_customer_at",
            "client_snapshot", "company_snapshot", "payment_method_snapshot",
            # Payment-receipt proof (set when the customer marks paid).
            "paid_at",
            "payment_proof_note",
            "payment_proof_url",
            "payment_proof_download_url",
            "payment_proof_filename",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "number",
            "customer",
            "subtotal", "tax_amount", "total",
            "issue_date",
            "share_token", "public_url", "pdf_url", "pdf_download_url",
            "pdf_theme",
            "expires_at", "first_viewed_at", "view_count",
            "sent_to_client_at", "sent_to_customer_at",
            "client_snapshot", "company_snapshot", "payment_method_snapshot",
            "status",
            "created_at", "updated_at",
            "client_name", "company_name", "payment_method_label",
            "invoice_payment_methods",
            # Proof fields are written exclusively via the mark-paid
            # action — never via the standard create/update endpoints.
            "paid_at",
            "payment_proof_note",
            "payment_proof_url",
            "payment_proof_download_url",
            "payment_proof_filename",
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

    def _public_pdf_ttl(self, obj):
        """TTL (seconds) for signed URLs on the public share page.

        Matches the invoice's ``expires_at`` exactly (option B chosen
        by the customer). If the invoice has no expiry set, falls
        back to the global default (``AWS_QUERYSTRING_EXPIRE``).
        """
        from django.conf import settings as dj_settings
        from django.utils import timezone
        default = int(dj_settings.AWS_QUERYSTRING_EXPIRE)
        if not obj.expires_at:
            return default
        remaining = int(
            (obj.expires_at - timezone.now()).total_seconds()
        )
        # Clamp to at least 60s — a URL that expires in under a
        # minute would 403 before the browser finishes the request.
        return max(60, remaining)

    def get_pdf_url(self, obj):
        """Inline preview URL for the <object> tag on the public page.

        For S3-hosted PDFs this is a pre-signed URL (no
        Content-Disposition header, so it renders inline); for old
        Cloudinary PDFs it's the raw Cloudinary URL.
        """
        if not obj.pdf_file:
            return None
        # S3 path: generate a plain signed URL with custom TTL.
        storage = obj.pdf_file.storage
        if hasattr(storage, "connection"):
            try:
                # Django-storages honours AWS_QUERYSTRING_EXPIRE
                # as a global, but for per-request TTL we sign by
                # hand through boto3.
                key = storage._normalize_name(
                    storage._clean_name(obj.pdf_file.name),
                )
                return storage.connection.meta.client.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={
                        "Bucket": storage.bucket_name,
                        "Key": key,
                    },
                    ExpiresIn=self._public_pdf_ttl(obj),
                )
            except Exception as e:
                log.warning("public inline S3 URL failed: %s", e)
        # Fallback (Cloudinary): the raw .url works as preview.
        req = self.context.get("request")
        try:
            raw = obj.pdf_file.url
            return req.build_absolute_uri(raw) if req else raw
        except Exception:
            return None

    def get_pdf_download_url(self, obj):
        """Download-forcing URL for the public page's Download button."""
        if not obj.pdf_file:
            return None
        # S3 path: signed URL with Content-Disposition, TTL bounded
        # by the invoice's expires_at.
        s3_url = _signed_s3_download_url(
            obj.pdf_file, obj.number, ttl=self._public_pdf_ttl(obj),
        )
        if s3_url:
            return s3_url
        # Cloudinary fallback.
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
