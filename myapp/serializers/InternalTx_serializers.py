"""Serializers for the internal-transactions module."""
from decimal import Decimal

from rest_framework import serializers

from myapp.Models.InternalTx_models import (
    Vendor, USABankAccount, CreditCard, InternalPakistaniAccount,
    InternalTransaction,
    InternalTxSource, InternalTxDestination,
)


# ---------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------

class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = [
            "id", "name",
            "contact_name", "contact_email", "contact_phone",
            "notes", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class USABankAccountSerializer(serializers.ModelSerializer):
    bank_display = serializers.CharField(
        source="get_bank_display", read_only=True,
    )

    class Meta:
        model = USABankAccount
        fields = [
            "id", "label", "bank", "bank_display",
            "holder_name", "account_number_last4",
            "notes", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "bank_display", "created_at", "updated_at"]


class CreditCardSerializer(serializers.ModelSerializer):
    brand_display = serializers.CharField(
        source="get_brand_display", read_only=True,
    )

    class Meta:
        model = CreditCard
        fields = [
            "id", "label", "brand", "brand_display",
            "last4", "holder_name",
            "notes", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "brand_display", "created_at", "updated_at"]


class InternalPakistaniAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = InternalPakistaniAccount
        fields = [
            "id", "label", "bank_name",
            "account_title", "account_number_last4", "iban",
            "notes", "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


# ---------------------------------------------------------------------
# Internal transaction
# ---------------------------------------------------------------------

class InternalTransactionSerializer(serializers.ModelSerializer):
    """Read + write serializer for InternalTransaction.

    Validation enforces the source/destination invariants:
      - source_type=usa_bank requires source_usa_bank, forbids source_credit_card
      - source_type=credit_card requires source_credit_card, forbids source_usa_bank
      - destination_type=usa_bank requires dest_usa_bank only
      - destination_type=vendor requires dest_vendor only
      - destination_type=pk_bank requires dest_pk_bank only
    Anything else is a 400.
    """
    source_label = serializers.CharField(read_only=True)
    destination_label = serializers.CharField(read_only=True)
    method_display = serializers.CharField(
        source="get_method_display", read_only=True,
    )
    source_type_display = serializers.CharField(
        source="get_source_type_display", read_only=True,
    )
    destination_type_display = serializers.CharField(
        source="get_destination_type_display", read_only=True,
    )
    currency_code = serializers.CharField(
        source="currency_id", read_only=True,
    )
    fee_currency_code = serializers.CharField(
        source="fee_currency_id", read_only=True,
    )
    fee_expense_id = serializers.PrimaryKeyRelatedField(
        source="fee_expense", read_only=True,
    )
    pk_fee_expense_id = serializers.PrimaryKeyRelatedField(
        source="pk_fee_expense", read_only=True,
    )
    fee_dist_partner_name = serializers.SerializerMethodField()
    created_by_email = serializers.CharField(
        source="created_by.email", read_only=True,
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True,
    )

    def get_fee_dist_partner_name(self, obj):
        if obj.fee_dist_partner_id and hasattr(obj, "fee_dist_partner") and obj.fee_dist_partner:
            return obj.fee_dist_partner.name
        return None

    class Meta:
        model = InternalTransaction
        fields = [
            "id",
            # Source
            "source_type", "source_type_display",
            "source_usa_bank", "source_credit_card",
            "source_label",
            # Destination
            "destination_type", "destination_type_display",
            "dest_usa_bank", "dest_vendor", "dest_pk_bank",
            "destination_label",
            # Money
            "currency", "currency_code", "amount",
            "fee_amount", "fee_currency", "fee_currency_code",
            "fee_expense_id",
            # Pakistani-bank side (USA→PK only)
            "pk_fee_percent", "pk_fee_amount",
            "pk_conversion_rate", "pk_amount_pkr",
            "pk_fee_expense_id",
            # Card-transaction dollar rate + PKR profit (credit_card source)
            "card_dollar_rate", "card_profit_pkr",
            "fee_dist_type", "fee_dist_partner_name", "fee_dist_partner", "fee_dist_partner_name",
            # Method + meta
            "method", "method_display",
            "reference", "description", "occurred_on",
            "document",
            # Bookkeeping
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "source_label", "destination_label",
            "method_display", "source_type_display", "destination_type_display",
            "currency_code", "fee_currency_code", "fee_expense_id",
            "pk_amount_pkr", "pk_fee_expense_id",
            "card_profit_pkr",
            "fee_dist_type", "fee_dist_partner_name",
            "created_by", "created_by_email", "created_by_name",
            "created_at", "updated_at",
        ]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self, data):
        # When PATCHing, the existing instance fills gaps left by the
        # partial payload. Build the merged view we want to validate.
        merged = {}
        if self.instance is not None:
            for f in (
                "source_type", "destination_type",
                "source_usa_bank", "source_credit_card",
                "dest_usa_bank", "dest_vendor", "dest_pk_bank",
                "amount", "fee_amount", "currency", "fee_currency",
                "card_dollar_rate",
            ):
                merged[f] = getattr(self.instance, f, None)
        merged.update(data)

        src = merged.get("source_type")
        dst = merged.get("destination_type")

        # ── Normalise stale FKs when the type is being changed ───────────
        # On a PATCH the instance may still hold the OLD source/destination
        # FK. If the caller changes the *type* without also blanking the old
        # FK (e.g. the bulk sheet flips destination_type usa_bank → pk_bank),
        # the merged view would carry a mismatched FK and fail the "must be
        # empty" checks below. So whenever a type is present in the incoming
        # payload, we explicitly null out the FKs that don't belong to the
        # new type — both in `data` (persisted) and `merged` (validated).
        def _clear(field):
            data[field] = None
            merged[field] = None

        if "source_type" in data:
            if src == InternalTxSource.USA_BANK and merged.get("source_credit_card"):
                _clear("source_credit_card")
            if src == InternalTxSource.CREDIT_CARD and merged.get("source_usa_bank"):
                _clear("source_usa_bank")
        if "destination_type" in data:
            keep = {
                InternalTxDestination.USA_BANK: "dest_usa_bank",
                InternalTxDestination.VENDOR:   "dest_vendor",
                InternalTxDestination.PK_BANK:  "dest_pk_bank",
            }.get(dst)
            for f in ("dest_usa_bank", "dest_vendor", "dest_pk_bank"):
                if f != keep and merged.get(f):
                    _clear(f)

        # ── Source validation ────────────────────────────────────────
        if src == InternalTxSource.USA_BANK:
            if not merged.get("source_usa_bank"):
                raise serializers.ValidationError({
                    "source_usa_bank":
                        "Required when source_type is 'usa_bank'.",
                })
            if merged.get("source_credit_card"):
                raise serializers.ValidationError({
                    "source_credit_card":
                        "Must be empty when source_type is 'usa_bank'.",
                })
        elif src == InternalTxSource.CREDIT_CARD:
            if not merged.get("source_credit_card"):
                raise serializers.ValidationError({
                    "source_credit_card":
                        "Required when source_type is 'credit_card'.",
                })
            if merged.get("source_usa_bank"):
                raise serializers.ValidationError({
                    "source_usa_bank":
                        "Must be empty when source_type is 'credit_card'.",
                })
        else:
            raise serializers.ValidationError({
                "source_type": "Unknown source type.",
            })

        # ── Destination validation ───────────────────────────────────
        # Helper to check that exactly the right destination FK is set.
        def require(field, allow_set):
            if not merged.get(field):
                raise serializers.ValidationError({
                    field:
                        f"Required when destination_type is '{dst}'.",
                })
            for other in ("dest_usa_bank", "dest_vendor", "dest_pk_bank"):
                if other not in allow_set and merged.get(other):
                    raise serializers.ValidationError({
                        other:
                            f"Must be empty when destination_type is '{dst}'.",
                    })

        if dst == InternalTxDestination.USA_BANK:
            require("dest_usa_bank", {"dest_usa_bank"})
        elif dst == InternalTxDestination.VENDOR:
            require("dest_vendor", {"dest_vendor"})
        elif dst == InternalTxDestination.PK_BANK:
            require("dest_pk_bank", {"dest_pk_bank"})
        else:
            raise serializers.ValidationError({
                "destination_type": "Unknown destination type.",
            })

        # ── A row can't move money to itself ─────────────────────────
        if (src == InternalTxSource.USA_BANK
                and dst == InternalTxDestination.USA_BANK):
            sub = merged.get("source_usa_bank")
            dub = merged.get("dest_usa_bank")
            # Compare PKs (both might be model instances or UUIDs).
            sub_id = getattr(sub, "pk", sub)
            dub_id = getattr(dub, "pk", dub)
            if sub_id and sub_id == dub_id:
                raise serializers.ValidationError({
                    "dest_usa_bank":
                        "Source and destination cannot be the same account.",
                })

        # ── Amount + fee sanity ──────────────────────────────────────
        amount = merged.get("amount")
        if amount is not None and Decimal(str(amount)) <= 0:
            raise serializers.ValidationError({
                "amount": "Amount must be greater than zero.",
            })
        fee = merged.get("fee_amount") or Decimal("0")
        if Decimal(str(fee)) < 0:
            raise serializers.ValidationError({
                "fee_amount": "Fee cannot be negative.",
            })

        # ── Pakistani-bank fee + conversion rate (USA→PK only) ───────────
        # Only meaningful when money lands in a PK bank. For other
        # destinations we simply ignore/zero them so a stray value can't
        # pollute reporting.
        pk_pct = data.get("pk_fee_percent", None)
        pk_amt = data.get("pk_fee_amount", None)
        pk_rate = data.get("pk_conversion_rate", None)

        if dst == InternalTxDestination.PK_BANK:
            if pk_pct is not None and Decimal(str(pk_pct)) < 0:
                raise serializers.ValidationError({
                    "pk_fee_percent": "PK fee percent cannot be negative.",
                })
            if pk_amt is not None and Decimal(str(pk_amt)) < 0:
                raise serializers.ValidationError({
                    "pk_fee_amount": "PK fee amount cannot be negative.",
                })
            if pk_rate is not None and Decimal(str(pk_rate)) <= 0:
                raise serializers.ValidationError({
                    "pk_conversion_rate":
                        "Conversion rate must be greater than zero.",
                })

            # Resolve the PK fee amount from the percentage when the caller
            # didn't supply an explicit override. amount × pct / 100.
            gross = merged.get("amount")
            if gross is not None:
                gross_d = Decimal(str(gross))
                if (pk_amt in (None, "", 0, "0") and pk_pct not in (None, "")):
                    data["pk_fee_amount"] = (
                        gross_d * Decimal(str(pk_pct)) / Decimal("100")
                    ).quantize(Decimal("0.01"))
                resolved_fee = Decimal(str(
                    data.get("pk_fee_amount",
                             merged.get("pk_fee_amount") or "0")
                ))
                # Net PKR landed = (gross − pk_fee) × rate.
                effective_rate = (
                    pk_rate
                    if pk_rate not in (None, "")
                    else merged.get("pk_conversion_rate")
                )
                if effective_rate not in (None, ""):
                    data["pk_amount_pkr"] = (
                        (gross_d - resolved_fee) * Decimal(str(effective_rate))
                    ).quantize(Decimal("0.01"))
        else:
            # Non-PK destination: never carry PK-side values.
            data["pk_fee_percent"] = Decimal("0")
            data["pk_fee_amount"] = Decimal("0")
            data["pk_conversion_rate"] = None
            data["pk_amount_pkr"] = None

        # ── Card-transaction dollar rate → PKR profit ────────────────────
        # For credit-card source transactions the rupee value of the spend
        # PLUS the bank fee — (amount + fee_amount) × card_dollar_rate — is
        # booked as company profit. The fee is still displayed as the bank
        # fee on the transaction, but for card payments it belongs to the
        # company (it is NOT pushed into Expenses — see the viewset's
        # `_sync_fee_expense`), so it counts toward profit at the same
        # dollar rate as the spend itself. For any other source we clear
        # the card_* fields so a stray value can't leak into profit
        # reporting.
        card_rate = data.get("card_dollar_rate", None)
        if src == InternalTxSource.CREDIT_CARD:
            if card_rate is not None and card_rate != "" and Decimal(str(card_rate)) < 0:
                raise serializers.ValidationError({
                    "card_dollar_rate": "Dollar rate cannot be negative.",
                })
            effective_rate = (
                card_rate
                if card_rate not in (None, "")
                else merged.get("card_dollar_rate")
            )
            gross = merged.get("amount")
            fee = merged.get("fee_amount")
            fee_d = Decimal(str(fee)) if fee not in (None, "") else Decimal("0")
            if fee_d < 0:
                fee_d = Decimal("0")
            if effective_rate not in (None, "") and gross is not None:
                data["card_profit_pkr"] = (
                    (Decimal(str(gross)) + fee_d) * Decimal(str(effective_rate))
                ).quantize(Decimal("0.01"))
            elif effective_rate in (None, ""):
                # No rate supplied → no card profit recorded.
                data["card_profit_pkr"] = None
        else:
            data["card_dollar_rate"] = None
            data["card_profit_pkr"] = None

        return data
