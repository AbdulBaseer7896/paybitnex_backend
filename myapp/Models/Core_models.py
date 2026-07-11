"""
Core lookup + config models.

Currency: USD and PKR (EUR/GBP deprecated — deactivated but retained for legacy rows).
SystemSetting: key-value store for admin-editable config
    (default fee %, rate buffer, bank list toggles, stale_payment_days, etc.)
PaymentMethod: admin-editable list of customer payment methods
    (Zelle, Cash App, ACH/Wire by default; admin can add more in Settings).
"""
from django.db import models


class Currency(models.Model):
    """Supported currencies. Code is the PK for natural joins."""
    code = models.CharField(max_length=3, primary_key=True)  # USD, PKR (EUR/GBP legacy)
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=8)
    is_active = models.BooleanField(default=True)
    is_base = models.BooleanField(
        default=False, help_text="True for PKR — the currency we pay out in.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "currencies"
        ordering = ["sort_order", "code"]
        verbose_name_plural = "Currencies"

    def __str__(self):
        return f"{self.code} ({self.symbol})"


class PaymentMethod(models.Model):
    """Methods by which customers receive foreign-currency payments.

    Seeded with Zelle, Cash App, ACH/Wire, Payoneer. Admin can add more
    from the Settings → Payment Methods page. Referenced by
    IncomingPayment.payment_method via its `code` so historical rows
    don't break if the admin renames something.

    The detail fields (`email`, `phone`, `account_number`, etc.) hold the
    merchant's own receiving details — what gets printed on invoices so
    the client knows where to send money. These are global defaults that
    apply to any customer who is granted access to the method via
    `CustomerAllowedPaymentMethod`.
    """

    # ---- basic identity ----
    code = models.CharField(max_length=32, primary_key=True)
    label = models.CharField(max_length=80)

    # ---- method family / type ----
    # `code` stays the stable primary key referenced by payments, invoices
    # and the customer allowed-methods table. But the admin can now create
    # MULTIPLE methods of the same kind — e.g. two separate Zelle accounts
    # (code "zelle", "zelle_2") each enrolled with a different USA bank.
    # `method_type` records which family a row belongs to so the UI can pick
    # the right icon/branding and group them, independent of the arbitrary
    # code. It is derived automatically from the code on save when left blank
    # (so existing rows and simple single-method setups keep working with no
    # change), and can be set explicitly when adding an extra account whose
    # code no longer literally spells out the family.
    TYPE_ZELLE    = "zelle"
    TYPE_CASHAPP  = "cashapp"
    TYPE_ACH_WIRE = "ach_wire"
    TYPE_PAYONEER = "payoneer"
    TYPE_OTHER    = "other"
    METHOD_TYPE_CHOICES = [
        (TYPE_ZELLE,    "Zelle"),
        (TYPE_CASHAPP,  "Cash App"),
        (TYPE_ACH_WIRE, "ACH / Wire"),
        (TYPE_PAYONEER, "Payoneer"),
        (TYPE_OTHER,    "Other"),
    ]
    method_type = models.CharField(
        max_length=32, choices=METHOD_TYPE_CHOICES, blank=True, default="",
        db_index=True,
        help_text="Payment-method family (Zelle, Cash App, …). Drives the "
                  "icon and grouping so multiple accounts of the same kind "
                  "can coexist. Auto-derived from the code when left blank.",
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="Show this method to all customers by default (pre-selected).",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    @staticmethod
    def derive_method_type(code):
        """Best-effort mapping from an arbitrary code to a known family.

        Uses substring matching so 'zelle_2', 'zelle_chase', 'cash_app_hq'
        all resolve correctly. Mirrors the frontend icon resolver and the
        Bank Statement page's `_norm_method` so the three stay consistent.
        """
        c = (code or "").lower().replace("-", "_").replace(" ", "_")
        if "zelle" in c:
            return PaymentMethod.TYPE_ZELLE
        if "cash" in c:            # cashapp / cash_app
            return PaymentMethod.TYPE_CASHAPP
        if "payoneer" in c:
            return PaymentMethod.TYPE_PAYONEER
        if "ach" in c or "wire" in c:
            return PaymentMethod.TYPE_ACH_WIRE
        return PaymentMethod.TYPE_OTHER

    # ---- receiving details (per-method; all optional so the admin can
    #      use whichever fields the method needs) ----

    # Zelle / Cash App style contact identifiers.
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    cashapp_tag = models.CharField(
        max_length=64, blank=True, default="",
        help_text="For Cash App, e.g. $freightflow",
    )

    # Bank account details for ACH / Wire / Payoneer.
    holder_name = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Account title / holder of record, e.g. 'Freight Flow Solutions'.",
    )
    account_number = models.CharField(max_length=40, blank=True, default="")
    routing_number = models.CharField(max_length=20, blank=True, default="")
    bank_name = models.CharField(max_length=120, blank=True, default="")
    account_type = models.CharField(
        max_length=40, blank=True, default="",
        help_text="E.g. 'Business Checking'.",
    )

    # Address of the account holder — printed on invoices / wire instructions.
    address_line1 = models.CharField(max_length=200, blank=True, default="")
    address_line2 = models.CharField(max_length=200, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=32, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")

    # Optional QR code image (admin uploads; appears on the invoice).
    qr_code = models.ImageField(
        upload_to="payment_methods/qr/", null=True, blank=True,
    )

    # Free-text extra instructions appended to the invoice payment section.
    instructions = models.TextField(
        blank=True, default="",
        help_text="Extra instructions the client should follow, e.g. "
                  "'Reference invoice # in memo'.",
    )

    # ── Bank-statement attribution ────────────────────────────────────
    # Which of OUR USA bank accounts money received via this method lands
    # in. This mirrors how US banking actually works: Zelle is enrolled to
    # exactly one bank account, Cash App's balance IS an account, and
    # ACH/Wire instructions point at a specific account & routing number.
    #
    # The Bank Statement page uses this to attribute every customer
    # payment to a bank, so "show me everything that hit Chase" includes
    # the Zelle receipts enrolled there. Nullable: methods without a
    # mapping show under "Unassigned" on the statement until the admin
    # sets one in Settings → Payment methods.
    deposit_account = models.ForeignKey(
        "myapp.USABankAccount", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="payment_methods",
        help_text="The company USA bank account where money received via "
                  "this method is deposited (e.g. Zelle → Chase Business).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "payment_methods"
        ordering = ["sort_order", "label"]
        constraints = [
            # One USA bank account maps to AT MOST one payment method.
            # The partial condition lets any number of methods stay
            # unmapped (deposit_account IS NULL) — only *assigned* banks
            # are forced to be unique. Django 5 emits this as a partial
            # unique index which SQLite supports natively.
            models.UniqueConstraint(
                fields=["deposit_account"],
                condition=models.Q(deposit_account__isnull=False),
                name="uniq_paymentmethod_deposit_account",
            ),
        ]

    def save(self, *args, **kwargs):
        # Auto-fill method_type from the code when the admin hasn't chosen
        # one explicitly. Keeps existing single-method setups working with
        # zero data changes, and gives new same-family accounts a sensible
        # default the admin can still override.
        if not self.method_type:
            self.method_type = self.derive_method_type(self.code)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.label


class SystemSetting(models.Model):
    """Admin-editable key-value config.

    Examples:
        default_fee_percentage    -> "5.00"
        min_transaction_amount    -> "10"
        rate_buffer_percentage    -> "1.5"
        require_email_screenshot  -> "true"
    """
    key = models.CharField(max_length=80, primary_key=True)
    value = models.TextField()
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "system_settings"
        ordering = ["key"]

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=None):
        try:
            return cls.objects.get(pk=key).value
        except cls.DoesNotExist:
            return default

    @classmethod
    async def aget(cls, key, default=None):
        try:
            obj = await cls.objects.aget(pk=key)
            return obj.value
        except cls.DoesNotExist:
            return default
