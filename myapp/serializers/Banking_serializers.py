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
