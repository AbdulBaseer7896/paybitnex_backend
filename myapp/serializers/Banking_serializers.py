"""Banking serializers: bank lookups + customer bank/merchant accounts."""
import re
from rest_framework import serializers
from myapp.Models.Banking_models import (
    PakistaniBank, ForeignBank,
    CustomerBankAccount, CustomerMerchantAccount,
)

_PK_IBAN_RE = re.compile(r"^PK\d{2}[A-Z0-9]{4}\d{16}$")
_GEN_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_PK_ACC_RE = re.compile(r"^\d{8,24}$")
_MERCHANT_ACC_RE = re.compile(r"^[A-Za-z0-9]{6,34}$")


def _iban_mod97_valid(iban: str) -> bool:
    """ISO 13616 Mod-97 checksum validation."""
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(
        str(ord(ch) - 55) if ch.isalpha() else ch
        for ch in rearranged
    )
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


def _validate_iban_value(value: str, pakistani: bool = False) -> str:
    cleaned = re.sub(r"[\s-]+", "", (value or "")).upper()
    if not cleaned:
        return ""
    if pakistani:
        if not _PK_IBAN_RE.match(cleaned):
            raise serializers.ValidationError(
                "Pakistani IBAN must be 24 characters (e.g. PK36SCBL0000001123456702)."
            )
    elif not _GEN_IBAN_RE.match(cleaned):
        raise serializers.ValidationError(
            "Enter a valid IBAN (15–34 alphanumeric characters starting with country code)."
        )
    if not _iban_mod97_valid(cleaned):
        raise serializers.ValidationError(
            "Invalid IBAN checksum. Please double-check the IBAN digits."
        )
    return cleaned


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
    on `field`. Excludes the account owner's own existing account.
    """
    if not value:
        return value
    qs = model.objects.filter(**{field: value})
    if instance is not None:
        qs = qs.exclude(pk=instance.pk)
    target_user = getattr(instance, "customer", None) or user
    if target_user is not None and getattr(target_user, "is_authenticated", False):
        qs = qs.exclude(customer=target_user)
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
        cleaned = re.sub(r"[\s-]+", "", (value or ""))
        if not _PK_ACC_RE.match(cleaned):
            raise serializers.ValidationError(
                "Account number must be 8 to 24 digits only."
            )
        user = self.context.get("request").user if self.context.get("request") else None
        return _check_unique_or_blank(
            CustomerBankAccount, "account_number", cleaned, self.instance, user=user,
        )

    def validate_iban(self, value):
        cleaned = _validate_iban_value(value, pakistani=True)
        user = self.context.get("request").user if self.context.get("request") else None
        return _check_unique_or_blank(
            CustomerBankAccount, "iban", cleaned, self.instance, user=user,
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
        cleaned = re.sub(r"[\s-]+", "", (value or "")).upper()
        if not _MERCHANT_ACC_RE.match(cleaned):
            raise serializers.ValidationError(
                "Account number must be 6 to 34 alphanumeric characters."
            )
        return _check_unique_or_blank(
            CustomerMerchantAccount, "account_number", cleaned, self.instance,
        )

    def validate_iban(self, value):
        cleaned = _validate_iban_value(value, pakistani=False)
        return _check_unique_or_blank(
            CustomerMerchantAccount, "iban", cleaned, self.instance,
        )
