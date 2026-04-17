"""
Per-customer fee configuration.

Admin sets a default fee % in SystemSettings; CustomerFeeConfig overrides
it for specific customers. The fee % is locked-in per transaction
(historical fee_percentage is stored on IncomingPayment directly).
"""
from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class CustomerFeeConfig(models.Model):
    customer = models.OneToOneField(
        "myapp.User", on_delete=models.CASCADE, related_name="fee_config",
        primary_key=True,
    )
    fee_percentage = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Overrides the system-default fee percentage.",
    )
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fee_config_updates",
    )

    class Meta:
        db_table = "customer_fee_configs"

    def __str__(self):
        return f"{self.customer.email}: {self.fee_percentage}%"
