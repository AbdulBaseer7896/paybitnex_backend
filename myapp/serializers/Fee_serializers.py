"""Fee configuration serializers."""
from rest_framework import serializers
from myapp.Models.Fee_models import CustomerFeeConfig


class CustomerFeeConfigSerializer(serializers.ModelSerializer):
    customer_email = serializers.CharField(source="customer.email", read_only=True)
    customer_name = serializers.CharField(source="customer.full_name", read_only=True)

    class Meta:
        model = CustomerFeeConfig
        fields = [
            "customer", "customer_email", "customer_name",
            "fee_percentage", "notes", "updated_at",
        ]
        read_only_fields = ["customer_email", "customer_name", "updated_at"]
