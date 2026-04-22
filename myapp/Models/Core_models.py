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

    Seeded with Zelle, Cash App, ACH/Wire. Admin can add more from the
    Settings page. Referenced by IncomingPayment.payment_method via its
    `code` so historical rows don't break if the admin renames something.
    """
    code = models.CharField(max_length=32, primary_key=True)
    label = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_methods"
        ordering = ["sort_order", "label"]

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
