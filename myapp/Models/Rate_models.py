"""
Exchange rate management.

ExchangeRate holds the CURRENT rate for each foreign currency → PKR.
ExchangeRateHistory is an append-only audit log of every rate change.

Live rates are fetched hourly by a Celery task. Admin/accountant can
override manually — overrides beat live fetches until cleared.
"""
from django.db import models


class ExchangeRate(models.Model):
    """One row per active source-currency → PKR pair."""
    SOURCE_LIVE = "live"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_LIVE, "Live API"),
        (SOURCE_MANUAL, "Manual Override"),
    ]

    currency = models.OneToOneField(
        "myapp.Currency", on_delete=models.CASCADE, to_field="code",
        related_name="rate", primary_key=True,
    )
    rate_to_pkr = models.DecimalField(max_digits=14, decimal_places=6)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_LIVE)
    manual_override_until = models.DateTimeField(
        null=True, blank=True,
        help_text="While set, live fetches won't overwrite this rate.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
    )

    class Meta:
        db_table = "exchange_rates"

    def __str__(self):
        return f"1 {self.currency_id} = {self.rate_to_pkr} PKR ({self.source})"


class ExchangeRateHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    currency_code = models.CharField(max_length=3, db_index=True)
    rate_to_pkr = models.DecimalField(max_digits=14, decimal_places=6)
    source = models.CharField(max_length=10)
    set_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "exchange_rate_history"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["currency_code", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.currency_code} @ {self.rate_to_pkr} ({self.created_at:%Y-%m-%d %H:%M})"
