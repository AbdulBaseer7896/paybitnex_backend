"""
Banking models:
- PakistaniBank / ForeignBank: seeded lookup tables
- CustomerBankAccount: customer's local (PK) account where PKR is received
- CustomerMerchantAccount: customer's foreign (US/UK/EU) account where they
  receive USD/EUR/GBP from senders
"""
import uuid
from django.db import models


class PakistaniBank(models.Model):
    """Lookup: list of banks operating in Pakistan (seeded)."""
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150, unique=True)
    short_code = models.CharField(max_length=20, blank=True)
    logo = models.ImageField(upload_to="banks/pk/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "banks_pk"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ForeignBank(models.Model):
    """Lookup: foreign banks (USA/UK/EU) for merchant accounts."""
    COUNTRY_USA = "USA"
    COUNTRY_UK = "UK"
    COUNTRY_EU = "EU"
    COUNTRY_CHOICES = [
        (COUNTRY_USA, "United States"),
        (COUNTRY_UK, "United Kingdom"),
        (COUNTRY_EU, "European Union"),
    ]

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150)
    country = models.CharField(max_length=10, choices=COUNTRY_CHOICES, db_index=True)
    logo = models.ImageField(upload_to="banks/foreign/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "banks_foreign"
        ordering = ["country", "name"]
        unique_together = [("name", "country")]

    def __str__(self):
        return f"{self.name} ({self.country})"


class CustomerBankAccount(models.Model):
    """Pakistani bank account — where customer receives PKR."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE, related_name="bank_accounts",
    )
    bank = models.ForeignKey(PakistaniBank, on_delete=models.PROTECT)
    holder_name = models.CharField(max_length=150)
    account_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        help_text="Must be unique across the whole system.",
    )
    iban = models.CharField(
        max_length=50, blank=True, help_text="IBAN (PK..) — unique if provided.",
    )

    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_bank_accounts"
        ordering = ["-is_primary", "-created_at"]

    def __str__(self):
        return f"{self.bank.name} • {self.account_number[-4:]}"


class CustomerMerchantAccount(models.Model):
    """Foreign (USA/UK/EU) account — where customer RECEIVES payments from senders."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE, related_name="merchant_accounts",
    )
    bank = models.ForeignKey(ForeignBank, on_delete=models.PROTECT)
    holder_name = models.CharField(max_length=150)
    account_number = models.CharField(
        max_length=80, unique=True, db_index=True,
        help_text="Must be unique across the whole system.",
    )
    iban = models.CharField(
        max_length=80, blank=True,
        help_text="IBAN / routing / SWIFT — unique if provided.",
    )
    routing_number = models.CharField(max_length=40, blank=True)
    swift_code = models.CharField(max_length=20, blank=True)

    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_merchant_accounts"
        ordering = ["-is_primary", "-created_at"]

    def __str__(self):
        return f"{self.bank.name} • {self.account_number[-4:]}"
