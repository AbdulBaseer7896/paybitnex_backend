"""Serializers for the dispatch module."""
import logging
from decimal import Decimal

from rest_framework import serializers

from myapp.Models.Dispatch_models import (
    DispatchCompany, DispatchDriver, Dispatch, DispatchStatus,
)

log = logging.getLogger(__name__)


class DispatchCompanySerializer(serializers.ModelSerializer):
    drivers_count = serializers.SerializerMethodField()
    loads_count = serializers.SerializerMethodField()

    class Meta:
        model = DispatchCompany
        fields = [
            "id", "name", "mc_number",
            "contact_name", "contact_email", "contact_phone",
            "address", "default_dispatch_fee_percent",
            "notes", "is_archived",
            "drivers_count", "loads_count",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_drivers_count(self, obj):
        # Use the prefetched count when present to avoid N+1.
        cached = getattr(obj, "_drivers_count", None)
        if cached is not None:
            return cached
        return obj.drivers.filter(is_archived=False).count()

    def get_loads_count(self, obj):
        cached = getattr(obj, "_loads_count", None)
        if cached is not None:
            return cached
        return obj.dispatches.count()


class DispatchDriverSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    truck_type_label = serializers.SerializerMethodField()

    class Meta:
        model = DispatchDriver
        fields = [
            "id", "company", "company_name",
            "name", "phone", "email", "license_number",
            "truck_type", "truck_type_label",
            "truck_number", "trailer_number",
            "notes", "is_archived",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "company_name", "created_at", "updated_at"]

    def get_truck_type_label(self, obj):
        return obj.get_truck_type_display() if obj.truck_type else ""

    def validate_company(self, value):
        # The view filters company queryset to the current customer, but
        # belt-and-suspenders: make sure the company is owned by the
        # current user.
        request = self.context.get("request")
        if request and value.customer_id != request.user.id:
            raise serializers.ValidationError(
                "Company does not belong to the current user.",
            )
        return value


class DispatchListSerializer(serializers.ModelSerializer):
    """Lighter shape for the dispatch list view."""
    company_name = serializers.SerializerMethodField()
    driver_name = serializers.SerializerMethodField()
    truck_type_label = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    has_invoice = serializers.SerializerMethodField()
    invoice_id = serializers.UUIDField(source="invoice.id", read_only=True, default=None)
    invoice_number = serializers.CharField(source="invoice.number", read_only=True, default=None)
    invoice_status = serializers.CharField(source="invoice.status", read_only=True, default=None)

    class Meta:
        model = Dispatch
        fields = [
            "id",
            "company", "company_name",
            "driver", "driver_name",
            "truck_type", "truck_type_label",
            "broker_name", "broker_phone",
            "load_number",
            "booked_date", "pickup_date", "delivery_date",
            "pickup_location", "dropoff_location",
            "loaded_miles",
            "rate", "dispatch_fee_percent", "dispatch_fee_flat", "dispatch_fee",
            "dispatcher_name",
            "status", "status_label",
            "is_paid", "paid_at",
            "has_invoice", "invoice_id", "invoice_number", "invoice_status",
            "created_at", "updated_at",
        ]

    def get_company_name(self, obj):
        try:
            return obj.company.name if obj.company_id else obj.company_name_snapshot
        except Exception:
            return obj.company_name_snapshot or ""

    def get_driver_name(self, obj):
        try:
            return obj.driver.name if obj.driver_id else obj.driver_name_snapshot
        except Exception:
            return obj.driver_name_snapshot or ""

    def get_truck_type_label(self, obj):
        return obj.get_truck_type_display() if obj.truck_type else ""

    def get_has_invoice(self, obj):
        return bool(obj.invoice_id)


class DispatchDetailSerializer(DispatchListSerializer):
    """Full shape for detail view + create/update."""
    rate_confirmation_url = serializers.SerializerMethodField()

    class Meta(DispatchListSerializer.Meta):
        fields = DispatchListSerializer.Meta.fields + [
            "broker_email", "broker_mc",
            "extra_stops", "deadhead_miles",
            "notes",
            "rate_confirmation", "rate_confirmation_url",
        ]
        extra_kwargs = {
            "rate_confirmation": {"write_only": True, "required": False},
        }

    def get_rate_confirmation_url(self, obj):
        if not obj.rate_confirmation:
            return None
        req = self.context.get("request")
        try:
            url = obj.rate_confirmation.url
            return req.build_absolute_uri(url) if req else url
        except Exception:
            return None

    def validate_company(self, value):
        request = self.context.get("request")
        if request and value.customer_id != request.user.id:
            raise serializers.ValidationError(
                "Company does not belong to the current user.",
            )
        return value

    def validate_driver(self, value):
        if not value:
            return value
        request = self.context.get("request")
        if request and value.customer_id != request.user.id:
            raise serializers.ValidationError(
                "Driver does not belong to the current user.",
            )
        return value

    def validate(self, attrs):
        # If both company and driver are provided, ensure the driver
        # actually belongs to the chosen company.
        company = attrs.get("company") or getattr(self.instance, "company", None)
        driver = attrs.get("driver", None)
        # `attrs.get("driver", "__missing__")` would be cleaner but DRF
        # passes None for cleared fields, so explicit check needed.
        if "driver" in attrs and driver and company and driver.company_id != company.id:
            raise serializers.ValidationError(
                {"driver": "Driver does not belong to the selected company."},
            )
        return attrs


class DispatchToInvoiceSerializer(serializers.Serializer):
    """Inputs for the 'Generate invoice for dispatch fee' action.

    The customer picks one of their invoicing Clients (the broker, or a
    consolidated billing entity) and one of their CustomerCompany
    letterheads. The view creates a single-line invoice for the
    dispatch fee amount.
    """
    client_id = serializers.UUIDField(required=True)
    company_id = serializers.UUIDField(required=True)
    payment_method_codes = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )
    due_date = serializers.DateField(required=False, allow_null=True)
    tax_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal("0"),
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
