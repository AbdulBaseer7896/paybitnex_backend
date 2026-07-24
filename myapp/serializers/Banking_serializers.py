"""Banking serializers: bank lookups + customer bank/merchant accounts."""
from rest_framework import serializers
from myapp.Models.Banking_models import (
    PakistaniBank, ForeignBank,
    CustomerBankAccount, CustomerMerchantAccount,
)


class PakistaniBankSerializer(serializers.ModelSerializer):
    class Meta:
        model = PakistaniBank
        fields = ["id", "name", "short_code", "logo", "is_active"]
        read_only_fields = fields


class ForeignBankSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForeignBank
        fields = ["id", "name", "country", "logo", "is_active"]
        read_only_fields = fields


def _check_unique_or_blank(model, field, value, instance, user=None):
    """
    If `value` is non-blank, ensure no other row in `model` has this value
    on `field`. Excludes the user's own existing account when user is provided.
    """
    if not value:
        return value
    qs = model.objects.filter(**{field: value})
    if instance is not None:
        qs = qs.exclude(pk=instance.pk)
    if user is not None and getattr(user, "is_authenticated", False):
        qs = qs.exclude(customer=user)
    if qs.exists():
        raise serializers.ValidationError(
            f"This {field.replace('_', ' ')} is already registered to another user."
        )
    return value


class CustomerBankAccountSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank.name", read_only=True)

    class Meta:
        model = CustomerBankAccount
        fields = [
            "id", "customer", "bank", "bank_name",
            "holder_name", "account_number", "iban",
            "is_primary", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "customer", "bank_name", "created_at", "updated_at"]

    def validate_account_number(self, value):
        user = self.context.get("request").user if self.context.get("request") else None
        return _check_unique_or_blank(
            CustomerBankAccount, "account_number", value, self.instance, user=user,
        )

    def validate_iban(self, value):
        user = self.context.get("request").user if self.context.get("request") else None
        return _check_unique_or_blank(
            CustomerBankAccount, "iban", value, self.instance, user=user,
        )


class CustomerMerchantAccountSerializer(serializers.ModelSerializer):
    bank_name = serializers.CharField(source="bank.name", read_only=True)
    bank_country = serializers.CharField(source="bank.country", read_only=True)

    class Meta:
        model = CustomerMerchantAccount
        fields = [
            "id", "customer", "bank", "bank_name", "bank_country",
            "holder_name", "account_number", "iban",
            "routing_number", "swift_code",
            "is_primary", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "customer", "bank_name", "bank_country",
            "created_at", "updated_at",
        ]

    def validate_account_number(self, value):
        return _check_unique_or_blank(
            CustomerMerchantAccount, "account_number", value, self.instance,
        )

    def validate_iban(self, value):
        return _check_unique_or_blank(
            CustomerMerchantAccount, "iban", value, self.instance,
        )
