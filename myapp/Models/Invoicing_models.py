"""
Invoicing module — customer-owned Clients and Companies.

These two models are the foundation of the invoicing flow. They belong to
the *customer* (our direct user) — not to the admin. Admin/accountant can
read them only through audit trails for dispute resolution, but they're
intentionally excluded from the main staff UIs per product spec.

- `Client`          — the customer's buyer/counterparty. One customer can
                      have many clients.
- `CustomerCompany` — the customer's own business. A customer can run
                      multiple businesses and bill each from its own
                      letterhead; one is marked primary so the invoice
                      form auto-selects it by default.
"""
import uuid

from django.db import models


class Client(models.Model):
    """A buyer/client that the customer invoices.

    Scoped to a single customer. No admin-side CRUD; admin can view for
    dispute resolution only via audit tooling.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="clients",
    )

    name = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")

    notes = models.TextField(
        blank=True, default="",
        help_text="Private notes the customer keeps about this client. "
                  "Not rendered on invoices.",
    )

    is_archived = models.BooleanField(
        default=False,
        help_text="Soft-delete flag. Archived clients stay linked to "
                  "existing invoices but disappear from the picker.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_clients"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "is_archived"]),
            models.Index(fields=["customer", "name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.customer_id})"


class CustomerCompany(models.Model):
    """A business that the customer invoices as.

    One customer can operate multiple companies — each has its own
    letterhead details (logo, address, contact). The `is_primary` flag
    marks the default used when creating a new invoice; we enforce the
    invariant that at most one company per customer is primary in the
    serializer/view layer (not via partial unique index, to stay portable
    across SQLite which doesn't support them cleanly).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="companies",
    )

    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    website = models.CharField(max_length=200, blank=True, default="")

    address_line1 = models.CharField(max_length=200, blank=True, default="")
    address_line2 = models.CharField(max_length=200, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=32, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")

    tax_id = models.CharField(
        max_length=64, blank=True, default="",
        help_text="EIN / VAT / GST number that should appear on the invoice.",
    )

    logo = models.ImageField(
        upload_to="customer_companies/logos/", null=True, blank=True,
    )

    is_primary = models.BooleanField(default=False)

    # Per-customer invoice settings. These sit here (rather than on User)
    # because different companies can have different preferences.
    invoice_link_expiry_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Number of days after which a public invoice link "
                  "expires. Null means never expire.",
    )
    invoice_number_prefix = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Optional prefix for auto-numbered invoices issued from "
                  "this company, e.g. 'ACME-'. Falls back to a generic "
                  "'INV-' prefix when blank.",
    )
    next_invoice_number = models.PositiveIntegerField(
        default=1,
        help_text="Auto-incremented sequence for invoices issued from "
                  "this company.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_companies"
        ordering = ["-is_primary", "name"]
        indexes = [
            models.Index(fields=["customer", "is_primary"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.customer_id})"


class CustomerAllowedPaymentMethod(models.Model):
    """Which payment methods each customer is allowed to offer on invoices.

    Admin controls this table — the customer cannot modify it. When
    creating an invoice, the customer's method picker is filtered to
    just the rows here. If the customer has zero allowed methods, the
    invoice can still be generated without a payment section (or with
    the admin-designated fallback primary).

    The `is_primary` flag marks the default method for this customer.
    If the customer doesn't pick a method when creating an invoice, we
    use the primary. Invariant: at most one row per customer has
    is_primary=True (enforced in the viewset, not a partial unique
    index, to stay portable across SQLite and MySQL).
    """
    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="allowed_payment_methods",
    )
    payment_method = models.ForeignKey(
        "myapp.PaymentMethod", on_delete=models.CASCADE,
        related_name="customer_grants",
    )
    is_primary = models.BooleanField(default=False)

    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="granted_payment_methods",
        help_text="Admin/staff user who granted access.",
    )

    class Meta:
        db_table = "customer_allowed_payment_methods"
        # One row per (customer, method) pair.
        unique_together = [("customer", "payment_method")]
        ordering = ["-is_primary", "payment_method__sort_order"]
        indexes = [
            models.Index(fields=["customer", "is_primary"]),
        ]

    def __str__(self):
        return f"{self.customer_id} -> {self.payment_method_id}"


class InvoiceStatus:
    DRAFT = "draft"
    SENT = "sent"
    VIEWED = "viewed"
    PAID = "paid"
    VOID = "void"
    CHOICES = [
        (DRAFT, "Draft"),
        (SENT, "Sent"),
        (VIEWED, "Viewed"),
        (PAID, "Paid"),
        (VOID, "Void"),
    ]


class Invoice(models.Model):
    """Invoice issued by a customer to one of their clients.

    Stores a snapshot of the company letterhead and client details at
    issue time so later edits to Client or CustomerCompany don't change
    what's printed on existing invoices. Line items live in a related
    `InvoiceLineItem` model.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Owner — the customer who issued the invoice.
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="invoices",
    )

    # Links — kept for filters/reports, but we also snapshot the data
    # below so deleted/edited clients/companies don't break old invoices.
    client = models.ForeignKey(
        "myapp.Client", on_delete=models.PROTECT,
        related_name="invoices",
    )
    company = models.ForeignKey(
        "myapp.CustomerCompany", on_delete=models.PROTECT,
        related_name="invoices",
    )
    payment_method = models.ForeignKey(
        "myapp.PaymentMethod", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="invoices",
        help_text="The payment method the client should use. Can be null "
                  "if the customer has no allowed methods — invoice is "
                  "then generated without a payment section.",
    )

    # Number — derived at create time from the company's prefix +
    # next_invoice_number, stored once so edits to the company's prefix
    # don't retroactively change historical invoices.
    number = models.CharField(max_length=40, db_index=True)

    # Money — USD only.
    currency_code = models.CharField(max_length=8, default="USD")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Tax applied to subtotal (0-100).",
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Dates.
    issue_date = models.DateField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)

    # Snapshots — the invoice is legally immutable once sent, so we
    # freeze the client/company details at create time.
    client_snapshot = models.JSONField(default=dict, blank=True)
    company_snapshot = models.JSONField(default=dict, blank=True)
    payment_method_snapshot = models.JSONField(default=dict, blank=True)

    # Description / notes.
    general_description = models.TextField(
        blank=True, default="",
        help_text="Free-form description above the line items.",
    )
    notes = models.TextField(
        blank=True, default="",
        help_text="Short footer note, e.g. 'Thanks for your business.'",
    )

    # Workflow.
    status = models.CharField(
        max_length=16, choices=InvoiceStatus.CHOICES,
        default=InvoiceStatus.DRAFT, db_index=True,
    )

    # Public sharing — token used for the no-auth public view URL. When
    # the customer's company's `invoice_link_expiry_days` is set, we
    # compute an absolute expiry at create time and enforce it at view
    # time. Null expiry means never expires (per customer's setting).
    share_token = models.CharField(
        max_length=48, unique=True, db_index=True,
        help_text="Random token for the public share link.",
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the public link stops working. Null = never.",
    )
    first_viewed_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)

    # Email send tracking.
    sent_to_client_at = models.DateTimeField(null=True, blank=True)
    sent_to_customer_at = models.DateTimeField(null=True, blank=True)

    # PDF — cached so repeated views don't re-generate; regenerated if
    # the invoice is edited before being sent.
    pdf_file = models.FileField(
        upload_to="invoices/pdf/", null=True, blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoices"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["client", "-created_at"]),
            models.Index(fields=["company", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.number} ({self.customer_id})"


class InvoiceLineItem(models.Model):
    """One row on an invoice (product/service line)."""
    id = models.BigAutoField(primary_key=True)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="line_items",
    )

    position = models.PositiveSmallIntegerField(
        default=0,
        help_text="Display order within the invoice.",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    quantity = models.DecimalField(
        max_digits=10, decimal_places=2, default=1,
    )
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )
    # Cached so we don't recompute on every serializer pass.
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        db_table = "invoice_line_items"
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        # Always keep total in sync with quantity * unit_price.
        from decimal import Decimal
        q = self.quantity or Decimal("0")
        p = self.unit_price or Decimal("0")
        self.total = (q * p).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.invoice_id}#{self.position}: {self.name}"


# Many-to-many through model for per-invoice payment method selection.
# An invoice can list multiple methods (e.g. "pay via Zelle OR ACH");
# admin controls which methods the customer has access to via
# CustomerAllowedPaymentMethod, but the customer chooses which subset
# to show on a given invoice.
class InvoicePaymentMethod(models.Model):
    """Links an Invoice to one of the PaymentMethods displayed on it."""
    id = models.BigAutoField(primary_key=True)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE,
        related_name="invoice_payment_methods",
    )
    payment_method = models.ForeignKey(
        "myapp.PaymentMethod", on_delete=models.PROTECT,
    )
    # Snapshot at invoice-creation time — master-data changes to the
    # payment method don't retroactively change what's printed on
    # already-issued invoices.
    snapshot = models.JSONField(default=dict, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "invoice_payment_methods"
        ordering = ["position", "id"]
        unique_together = [("invoice", "payment_method")]
