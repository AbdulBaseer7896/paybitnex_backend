"""
Internal-transactions module.

Tracks the company's *own* money movement — separate from the customer
flow handled by `Transaction_models.py`. Captures four scenarios:

  1. USA bank → USA bank        (e.g. CashApp out → US Bank)
  2. USA bank → Vendor          (paying a vendor in the US)
  3. USA bank → PK bank         (sending company funds home)
  4. Credit card → Vendor       (using a credit card to pay a vendor)

Every transaction can carry a per-transaction transfer fee. When a fee
is recorded, an `Expense` row is auto-created in the BANKING category
and linked back via `InternalTransaction.fee_expense`. That keeps the
"net profit" number on the dashboard correct without the admin having
to remember to log the fee separately.

Design notes:
  - Source / destination use a (type, id) pair instead of polymorphic
    FKs so the admin UI can render the right picker per type, and so
    foreign-key cascades stay simple per source/destination table.
  - Vendors / USABankAccount / CreditCard / InternalPakistaniAccount
    are *admin*-managed reference data — the same data the admin
    enters in the new tabs under Account Settings. They're company
    assets, not customer assets, so they intentionally don't reuse
    `CustomerBankAccount` / `CustomerMerchantAccount`.
"""
import uuid

from django.db import models


# ---------------------------------------------------------------------
# Reference data (admin-managed)
# ---------------------------------------------------------------------

class Vendor(models.Model):
    """A vendor / supplier the company pays.

    Free-form: the admin types a name and a few optional details.
    No foreign key from the customer side — vendors here are purely
    accounts-payable counterparties for the company itself.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    contact_name = models.CharField(max_length=150, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=32, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "internal_vendors"
        ordering = ["name"]

    def __str__(self):
        return self.name


class USABankAccount(models.Model):
    """A US-side bank account the company controls.

    Seeded with the five live ones — US Bank, American Express,
    Cash App, Airwallex, Chase — but the admin can add more from the
    Account Settings → USA bank accounts tab. We store the masked
    last-4 only; the full account number doesn't need to be in the
    admin UI for an internal-transaction picker.
    """
    BANK_USBANK     = "us_bank"
    BANK_AMEX       = "amex"
    BANK_CASHAPP    = "cashapp"
    BANK_AIRWALLEX  = "airwallex"
    BANK_CHASE      = "chase"
    BANK_OTHER      = "other"
    BANK_CHOICES = [
        (BANK_USBANK,    "US Bank"),
        (BANK_AMEX,      "American Express"),
        (BANK_CASHAPP,   "Cash App"),
        (BANK_AIRWALLEX, "Airwallex"),
        (BANK_CHASE,     "Chase"),
        (BANK_OTHER,     "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(
        max_length=120,
        help_text="Friendly label shown in the picker, e.g. 'Chase Business'.",
    )
    bank = models.CharField(
        max_length=20, choices=BANK_CHOICES, default=BANK_OTHER, db_index=True,
    )
    holder_name = models.CharField(max_length=150, blank=True, default="")
    account_number_last4 = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Account number — full number or last digits, "
                  "your choice. Used for disambiguation in the picker.",
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "internal_usa_bank_accounts"
        ordering = ["bank", "label"]

    def __str__(self):
        # Show the last 4 digits in the picker so even very long
        # account numbers stay readable. If the admin entered fewer
        # than 4 chars (or used a label-only entry), fall back to
        # the full stored value.
        digits = self.account_number_last4 or ""
        suffix = (f" ••{digits[-4:]}"
                  if len(digits) >= 4
                  else (f" ({digits})" if digits else ""))
        return f"{self.label}{suffix}"


class CreditCard(models.Model):
    """A US credit card the company uses to pay vendors.

    Stored only for picker labelling — last 4 digits, brand, holder.
    Full numbers are NOT stored; this isn't a payments processor.
    """
    BRAND_VISA       = "visa"
    BRAND_MASTERCARD = "mastercard"
    BRAND_AMEX       = "amex"
    BRAND_DISCOVER   = "discover"
    BRAND_OTHER      = "other"
    BRAND_CHOICES = [
        (BRAND_VISA,       "Visa"),
        (BRAND_MASTERCARD, "Mastercard"),
        (BRAND_AMEX,       "American Express"),
        (BRAND_DISCOVER,   "Discover"),
        (BRAND_OTHER,      "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=120)
    brand = models.CharField(
        max_length=16, choices=BRAND_CHOICES, default=BRAND_OTHER,
    )
    last4 = models.CharField(
        max_length=19, blank=True, default="",
        help_text="Card number — typically 15 (Amex) or 16 digits. "
                  "Spaces are allowed.",
    )
    holder_name = models.CharField(max_length=150, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "internal_credit_cards"
        ordering = ["label"]

    def __str__(self):
        # Show the last 4 digits of the card in the picker — same
        # rationale as USABankAccount.
        digits = (self.last4 or "").replace(" ", "")
        suffix = (f" ••{digits[-4:]}"
                  if len(digits) >= 4
                  else (f" ({digits})" if digits else ""))
        return f"{self.label}{suffix}"


class InternalPakistaniAccount(models.Model):
    """Internal PK bank accounts owned by the company.

    Distinct from `CustomerBankAccount` (which is per-customer, used
    for the customer payment-out flow). These are *our* accounts —
    where USA→PK transfers land.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=150)
    bank_name = models.CharField(max_length=150)
    account_title = models.CharField(max_length=150, blank=True, default="")
    account_number_last4 = models.CharField(max_length=64, blank=True, default="")
    iban = models.CharField(max_length=64, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "internal_pk_accounts"
        ordering = ["label"]

    def __str__(self):
        digits = self.account_number_last4 or ""
        suffix = (f" ••{digits[-4:]}"
                  if len(digits) >= 4
                  else (f" ({digits})" if digits else ""))
        return f"{self.label} ({self.bank_name}){suffix}"


# ---------------------------------------------------------------------
# The transaction itself
# ---------------------------------------------------------------------

class InternalTxSource:
    """Where the money came from."""
    USA_BANK    = "usa_bank"
    CREDIT_CARD = "credit_card"
    CHOICES = [
        (USA_BANK,    "USA bank account"),
        (CREDIT_CARD, "Credit card"),
    ]


class InternalTxDestination:
    """Where the money went."""
    USA_BANK = "usa_bank"
    VENDOR   = "vendor"
    PK_BANK  = "pk_bank"
    CHOICES = [
        (USA_BANK, "USA bank account"),
        (VENDOR,   "Vendor"),
        (PK_BANK,  "Pakistani bank account"),
    ]


class InternalTxMethod:
    """How the money moved."""
    WIRE = "wire"
    ACH  = "ach"
    CARD = "card"
    OTHER = "other"
    CHOICES = [
        (WIRE,  "Wire"),
        (ACH,   "ACH"),
        (CARD,  "Card"),
        (OTHER, "Other / internal"),
    ]


class InternalTransaction(models.Model):
    """A single internal money movement.

    Source + destination are stored as a discriminator + nullable FKs
    to each possible side. Exactly one source FK and one destination FK
    are populated per row, matching the discriminator. Validation lives
    in the serializer (not as a check constraint, so MySQL/SQLite stay
    happy across environments).

    When ``fee_amount > 0`` is set, the viewset's ``perform_create`` /
    ``perform_update`` will auto-create or sync an `Expense` row in
    the BANKING category and link it back via ``fee_expense``. That
    expense becomes the single source of truth for the fee in the
    dashboard's net-profit calculation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Discriminators
    source_type = models.CharField(
        max_length=20, choices=InternalTxSource.CHOICES, db_index=True,
    )
    destination_type = models.CharField(
        max_length=20, choices=InternalTxDestination.CHOICES, db_index=True,
    )

    # Source side — nullable FKs, exactly one populated per source_type.
    source_usa_bank = models.ForeignKey(
        USABankAccount, on_delete=models.PROTECT,
        null=True, blank=True, related_name="outgoing_transactions",
    )
    source_credit_card = models.ForeignKey(
        CreditCard, on_delete=models.PROTECT,
        null=True, blank=True, related_name="outgoing_transactions",
    )

    # Destination side — nullable FKs, exactly one populated per destination_type.
    dest_usa_bank = models.ForeignKey(
        USABankAccount, on_delete=models.PROTECT,
        null=True, blank=True, related_name="incoming_transactions",
    )
    dest_vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT,
        null=True, blank=True, related_name="payments_received",
    )
    dest_pk_bank = models.ForeignKey(
        InternalPakistaniAccount, on_delete=models.PROTECT,
        null=True, blank=True, related_name="incoming_transactions",
    )

    # Money
    currency = models.ForeignKey(
        "myapp.Currency", on_delete=models.PROTECT, to_field="code",
        related_name="internal_transactions",
    )
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="Gross amount transferred, in `currency`.",
    )
    fee_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text="Bank / wire / processing fee charged on this "
                  "transfer, in `currency`. For bank transfers it is "
                  "auto-pushed into Expenses in the BANKING category. "
                  "For CREDIT-CARD transactions the fee is NOT an expense: "
                  "it is converted along with the spend and folded into "
                  "card_profit_pkr, which represents PKR RECEIVED into our "
                  "Pakistani banks (not company profit).",
    )
    fee_currency = models.ForeignKey(
        "myapp.Currency", on_delete=models.PROTECT, to_field="code",
        related_name="internal_transaction_fees",
        null=True, blank=True,
        help_text="Currency the fee was charged in. Usually the same "
                  "as `currency`; left null = inherit from `currency`.",
    )

    # ── Pakistani-bank side (only relevant for USA bank → PK bank) ──────
    # The receiving PK bank charges its own fee, expressed as a percentage
    # of the gross transferred amount (default 0.25%, but editable per
    # transfer). We store BOTH the percentage and the resolved fee amount
    # so historical rows stay correct even if the default rate changes.
    pk_fee_percent = models.DecimalField(
        max_digits=6, decimal_places=4, default=0,
        help_text="Pakistani bank fee as a percent of the gross amount "
                  "(e.g. 0.25 = 0.25%). Only used for USA→PK transfers.",
    )
    pk_fee_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
        help_text="Resolved PK bank fee in `currency` (amount × "
                  "pk_fee_percent / 100). Auto-pushed into Expenses in "
                  "the BANKING category, like the USA-side fee.",
    )
    # The dollar rate the PK bank gave us, i.e. 1 unit of `currency` = N PKR.
    # Used to compute how many PKR actually landed.
    pk_conversion_rate = models.DecimalField(
        max_digits=14, decimal_places=6, null=True, blank=True,
        help_text="PKR the receiving bank paid per 1 unit of `currency` "
                  "(1 USD = N PKR). Only used for USA→PK transfers.",
    )
    pk_amount_pkr = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="Net PKR that landed: (amount − pk_fee_amount) × "
                  "pk_conversion_rate. Stored for reporting.",
    )
    # Separate Expense link for the PK-side fee (the USA-side fee uses
    # `fee_expense`). Kept distinct so each fee is its own auditable row.
    pk_fee_expense = models.ForeignKey(
        "myapp.Expense", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="internal_transaction_pk_fees",
    )

    # ── Card-transaction dollar rate + PKR RECEIVED ────────────────────
    # For CREDIT CARD source transactions the company spends foreign
    # currency on the card, and the rupee value of that spend lands in our
    # Pakistani banks. We store the rate used (1 unit of `currency` = N PKR)
    # and the resolved rupee amount.
    #
    # IMPORTANT: this is RECEIVED MONEY, not company profit. It joins the
    # same rupee pool as USA→PK bank transfers (pk_amount_pkr) and funds
    # customer payouts. It must never be added to any profit total. The
    # column keeps its historical `card_profit_pkr` name to avoid a data
    # migration; the API exposes it as `card_received_pkr`.
    card_dollar_rate = models.DecimalField(
        max_digits=14, decimal_places=6, null=True, blank=True,
        help_text="PKR value per 1 unit of `currency` for a card "
                  "transaction (1 USD = N PKR). Only used when "
                  "source_type = 'credit_card'.",
    )
    card_profit_pkr = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="PKR RECEIVED into our Pakistani banks from this card "
                  "transaction: (amount + fee_amount) × card_dollar_rate. "
                  "Despite the legacy column name this is NOT company "
                  "profit — it is part of the PKR reconciliation pool, "
                  "alongside USA→PK bank transfers. Exposed by the API as "
                  "`card_received_pkr`.",
    )

    # Method
    method = models.CharField(
        max_length=16, choices=InternalTxMethod.CHOICES,
        default=InternalTxMethod.OTHER, db_index=True,
    )

    # Context
    reference = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Bank reference / wire ID / memo line.",
    )
    description = models.TextField(blank=True, default="")
    occurred_on = models.DateField(
        db_index=True,
        help_text="Date the transaction was initiated.",
    )

    # Optional proof document (receipt / wire confirmation)
    document = models.FileField(
        upload_to="internal_transactions/docs/", null=True, blank=True,
    )

    # Auto-managed link to the Expense row that holds the fee. We use
    # SET_NULL so deleting the expense by hand doesn't cascade-delete
    # the transaction; the viewset will recreate the expense on next
    # save if the link breaks.
    fee_expense = models.ForeignKey(
        "myapp.Expense", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="internal_transaction_fees",
    )

    FEE_DIST_COMPANY = "company"
    FEE_DIST_PARTNER = "partner"
    FEE_DIST_CUSTOM  = "custom"
    FEE_DIST_CHOICES = [
        (FEE_DIST_COMPANY, "Company"),
        (FEE_DIST_PARTNER, "Single partner"),
        (FEE_DIST_CUSTOM,  "Custom split (see expense distributions)"),
    ]

    fee_dist_type = models.CharField(
        max_length=10, choices=FEE_DIST_CHOICES, default=FEE_DIST_COMPANY,
        help_text="Who absorbs this transfer fee.",
    )
    fee_dist_partner = models.ForeignKey(
        "myapp.Partner", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="internal_tx_fees",
        help_text="Partner who absorbs the fee (when fee_dist_type=partner).",
    )

    # Bookkeeping
    created_by = models.ForeignKey(
        "myapp.User", on_delete=models.PROTECT,
        related_name="created_internal_transactions",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "internal_transactions"
        ordering = ["-occurred_on", "-created_at"]
        indexes = [
            models.Index(fields=["occurred_on", "source_type"]),
            models.Index(fields=["occurred_on", "destination_type"]),
            models.Index(fields=["currency", "occurred_on"]),
        ]

    def __str__(self):
        return f"{self.amount} {self.currency_id} on {self.occurred_on}"

    # ------------------------------------------------------------------
    # Display helpers — used by the serializer to populate readable
    # source/destination labels without the frontend having to join.
    # ------------------------------------------------------------------
    def source_label(self):
        if self.source_type == InternalTxSource.USA_BANK and self.source_usa_bank_id:
            return str(self.source_usa_bank)
        if self.source_type == InternalTxSource.CREDIT_CARD and self.source_credit_card_id:
            return str(self.source_credit_card)
        return ""

    def destination_label(self):
        if self.destination_type == InternalTxDestination.USA_BANK and self.dest_usa_bank_id:
            return str(self.dest_usa_bank)
        if self.destination_type == InternalTxDestination.VENDOR and self.dest_vendor_id:
            return str(self.dest_vendor)
        if self.destination_type == InternalTxDestination.PK_BANK and self.dest_pk_bank_id:
            return str(self.dest_pk_bank)
        return ""
