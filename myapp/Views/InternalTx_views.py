"""
Internal-transactions views.

Five viewsets:
  - VendorViewSet
  - USABankAccountViewSet
  - CreditCardViewSet
  - InternalPakistaniAccountViewSet
  - InternalTransactionViewSet  (with auto-Expense fee linkage)

All admin/accountant-only. Customers don't have access — these are
the company's *own* finances.

The InternalTransaction viewset's `_sync_fee_expense` helper is the
piece that wires the fee field into the Expense table. It runs after
every create and update so the Expense row stays in sync, and on
destroy it removes the linked expense alongside the transaction.
"""
from decimal import Decimal

from django.db import transaction as dbtx
from django.db.models import Sum, Count, Q

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.Models.Audit_models import AuditLog
from myapp.Models.Expense_models import Expense, ExpenseCategory
from myapp.Models.InternalTx_models import (
    Vendor, USABankAccount, CreditCard, InternalPakistaniAccount,
    InternalTransaction, VendorPKRPayment, InternalTxDestination,
)
from myapp.serializers.InternalTx_serializers import (
    VendorSerializer, USABankAccountSerializer, CreditCardSerializer,
    InternalPakistaniAccountSerializer, InternalTransactionSerializer,
    VendorPKRPaymentSerializer,
)
from myapp.Utils.permissions import IsAdmin, IsAdminOrAccountant


# ---------------------------------------------------------------------
# Reference-data viewsets (admin manages, accountant reads)
# ---------------------------------------------------------------------

class _BaseRefViewSet(viewsets.ModelViewSet):
    """Shared base for the four reference-data tables.

    - Admin can create/update/delete.
    - Accountant can create + read (so the "add new bank inline" flow
      on the New Internal Transaction modal works for accountants too)
      but cannot update or destroy persisted reference data.
    - Customers have no access (the URL is mounted under the
      admin-side internal-transactions namespace).
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    audit_label = "ref"

    def get_permissions(self):
        if self.action in (
            "update", "partial_update", "destroy", "pkr_converters",
        ):
            return [IsAuthenticated(), IsAdmin()]
        # create + list + retrieve all available to admin AND accountant
        return [IsAuthenticated(), IsAdminOrAccountant()]

    def perform_create(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"{self.audit_label} created: {obj}",
        )

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"{self.audit_label} updated: {obj}",
        )

    def perform_destroy(self, instance):
        label = str(instance)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_DELETE,
            target=instance,
            description=f"{self.audit_label} deleted: {label}",
        )
        instance.delete()


class VendorViewSet(_BaseRefViewSet):
    """Admin-managed vendor list."""
    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer
    audit_label = "Vendor"
    search_fields = ["name", "contact_name", "contact_email"]
    filterset_fields = ["is_active", "handles_pkr_conversion"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]

    @action(
        detail=False,
        methods=["get", "patch"],
        url_path="pkr-converters",
        permission_classes=[IsAuthenticated, IsAdmin],
    )
    def pkr_converters(self, request):
        """Read or replace the set of vendors enabled for PKR conversion."""
        if request.method == "GET":
            rows = self.get_queryset().filter(handles_pkr_conversion=True)
            return Response({
                "vendor_ids": [str(v.id) for v in rows],
                "vendors": VendorSerializer(rows, many=True).data,
            })

        vendor_ids = request.data.get("vendor_ids")
        if not isinstance(vendor_ids, list):
            return Response(
                {"vendor_ids": "Provide a list of vendor IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        vendor_ids = list(dict.fromkeys(str(value) for value in vendor_ids))
        try:
            selected = list(Vendor.objects.filter(pk__in=vendor_ids))
        except (TypeError, ValueError):
            return Response(
                {"vendor_ids": "One or more vendor IDs are invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(selected) != len(vendor_ids):
            return Response(
                {"vendor_ids": "One or more selected vendors do not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        before = list(
            Vendor.objects.filter(handles_pkr_conversion=True)
            .values_list("name", flat=True)
        )
        with dbtx.atomic():
            Vendor.objects.filter(handles_pkr_conversion=True).update(
                handles_pkr_conversion=False,
            )
            Vendor.objects.filter(pk__in=vendor_ids).update(
                handles_pkr_conversion=True,
            )
        after = [vendor.name for vendor in selected]
        AuditLog.record(
            user=request.user,
            action=AuditLog.ACTION_UPDATE,
            target_label="PKR converter vendors",
            description="Updated the vendors eligible for PKR conversion.",
            before={"vendors": before},
            after={"vendors": after},
        )
        rows = self.get_queryset().filter(handles_pkr_conversion=True)
        return Response({
            "vendor_ids": [str(v.id) for v in rows],
            "vendors": VendorSerializer(rows, many=True).data,
        })


class USABankAccountViewSet(_BaseRefViewSet):
    """Admin-managed USA bank accounts (US Bank, Amex, Cash App, etc.)."""
    queryset = USABankAccount.objects.all()
    serializer_class = USABankAccountSerializer
    audit_label = "USA bank account"
    search_fields = ["label", "holder_name", "account_number_last4"]
    filterset_fields = ["bank", "is_active"]
    ordering_fields = ["label", "bank", "created_at"]
    ordering = ["bank", "label"]


class CreditCardViewSet(_BaseRefViewSet):
    """Admin-managed credit card list."""
    queryset = CreditCard.objects.select_related("linked_usa_bank").all()
    serializer_class = CreditCardSerializer
    audit_label = "Credit card"
    search_fields = ["label", "holder_name", "last4"]
    filterset_fields = ["brand", "is_active", "linked_usa_bank"]
    ordering_fields = ["label", "brand", "created_at"]
    ordering = ["label"]


class InternalPakistaniAccountViewSet(_BaseRefViewSet):
    """Admin-managed Pakistani bank accounts the company owns."""
    queryset = InternalPakistaniAccount.objects.all()
    serializer_class = InternalPakistaniAccountSerializer
    audit_label = "Internal PK account"
    search_fields = ["label", "bank_name", "account_title", "iban"]
    filterset_fields = ["is_active"]
    ordering_fields = ["label", "bank_name", "created_at"]
    ordering = ["label"]


class VendorPKRPaymentViewSet(viewsets.ModelViewSet):
    """Admin/accountant ledger for PKR received from USD converters."""
    queryset = (
        VendorPKRPayment.objects
        .select_related("vendor", "pk_bank_account", "created_by")
        .all()
    )
    serializer_class = VendorPKRPaymentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrAccountant]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["vendor"]
    search_fields = [
        "vendor__name", "confirmation_code", "bank_transaction_id", "notes",
    ]
    ordering_fields = [
        "occurred_on", "created_at", "pkr_received", "usd_sent",
        "pkr_equivalent", "balance_pkr",
    ]
    ordering = ["-occurred_on", "-created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("date_from"):
            qs = qs.filter(occurred_on__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(occurred_on__lte=params["date_to"])
        return qs

    def perform_create(self, serializer):
        payment = serializer.save(created_by=self.request.user)

        tx_ids_raw = self.request.data.get("transaction_ids")
        if tx_ids_raw:
            import json
            try:
                tx_ids = json.loads(tx_ids_raw)
            except Exception:
                tx_ids = [x.strip() for x in str(tx_ids_raw).split(",") if x.strip()]

            if tx_ids:
                # A USD→PKR conversion only exists when the money left the
                # US — a USA→USA bank transfer never converts, so stamping
                # a converter on one would invent a conversion that never
                # happened and double-count the USD in the vendor ledger.
                txs = InternalTransaction.objects.filter(
                    id__in=tx_ids,
                    destination_type__in=[
                        InternalTxDestination.VENDOR,
                        InternalTxDestination.PK_BANK,
                    ],
                )
                for tx in txs:
                    tx.pkr_converter_vendor = payment.vendor
                    tx.linked_vendor_pkr_payment = payment
                    # If this is a PK bank transfer, ensure the destination bank matches where PKR landed
                    if payment.pk_bank_account_id and tx.destination_type == 'pk_bank':
                        tx.dest_pk_bank = payment.pk_bank_account
                    tx.save()

        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=payment,
            description=f"Vendor PKR payment recorded: {payment}",
        )

    def perform_update(self, serializer):
        payment = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=payment,
            description=f"Vendor PKR payment updated: {payment}",
        )

    def perform_destroy(self, instance):
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_DELETE,
            target=instance,
            description=f"Vendor PKR payment deleted: {instance}",
        )
        instance.delete()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        qs = self.filter_queryset(self.get_queryset()).order_by()
        totals = qs.aggregate(
            pkr_received=Sum("pkr_received"),
            usd_sent=Sum("usd_sent"),
            pkr_equivalent=Sum("pkr_equivalent"),
            balance_pkr=Sum("balance_pkr"),
            count=Count("id"),
        )
        by_vendor = list(
            qs.values("vendor_id", "vendor__name")
            .annotate(
                pkr_received=Sum("pkr_received"),
                usd_sent=Sum("usd_sent"),
                pkr_equivalent=Sum("pkr_equivalent"),
                balance_pkr=Sum("balance_pkr"),
                count=Count("id"),
            )
            .order_by("vendor__name")
        )
        def serialise(row):
            return {
                key: (str(value) if isinstance(value, Decimal) else value or 0)
                for key, value in row.items()
            }
        return Response({
            "totals": serialise(totals),
            "by_vendor": [serialise(row) for row in by_vendor],
        })


# ---------------------------------------------------------------------
# Internal transaction viewset
# ---------------------------------------------------------------------

class InternalTransactionViewSet(viewsets.ModelViewSet):
    """
    GET    /internal-transactions/transactions/        - list
    POST   /internal-transactions/transactions/        - create
    GET    /internal-transactions/transactions/{id}/   - detail
    PATCH  /internal-transactions/transactions/{id}/   - update
    DELETE /internal-transactions/transactions/{id}/   - delete
    GET    /internal-transactions/transactions/summary/ - aggregates

    Admin can do everything. Accountant can read + create + update,
    but not delete (deletes are sensitive because they cascade to
    the linked fee Expense row).

    Fee-to-Expense sync:
        After every create/update, ``_sync_fee_expense`` is called.
        - fee_amount > 0 + no fee_expense yet → create one
        - fee_amount > 0 + existing fee_expense → update it in place
        - fee_amount == 0 + existing fee_expense → delete the expense
        The expense is created in the BANKING category with the
        transaction's vendor or destination as the vendor name.
    """
    queryset = (InternalTransaction.objects
                .select_related(
                    "currency", "fee_currency", "created_by",
                    "source_usa_bank", "source_credit_card",
                    "source_credit_card__linked_usa_bank",
                    "dest_usa_bank", "dest_vendor", "dest_pk_bank",
                    "fee_expense", "fee_dist_partner",
                ))
    serializer_class = InternalTransactionSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = [
        "source_type", "destination_type", "method", "currency",
        "source_usa_bank", "source_credit_card",
        "dest_usa_bank", "dest_vendor", "dest_pk_bank",
    ]
    search_fields = ["reference", "description"]
    ordering_fields = ["occurred_on", "created_at", "amount", "fee_amount"]
    ordering = ["-occurred_on", "-created_at"]

    def get_permissions(self):
        if self.action == "destroy":
            return [IsAuthenticated(), IsAdmin()]
        return [IsAuthenticated(), IsAdminOrAccountant()]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("date_from"):
            qs = qs.filter(occurred_on__gte=p.get("date_from"))
        if p.get("date_to"):
            qs = qs.filter(occurred_on__lte=p.get("date_to"))
        return qs

    # ------------------------------------------------------------------
    # Fee → Expense sync
    # ------------------------------------------------------------------
    def _fee_title(self, tx):
        """Human-readable title for the auto-generated Expense row."""
        method = tx.get_method_display() or "transfer"
        # Prefer a destination-flavoured title because that's what
        # the admin remembers ("wire fee for ACME vendor payment").
        dest = tx.destination_label() or tx.source_label() or "internal transfer"
        return f"{method} fee — {dest}"[:200]

    def _fee_vendor_name(self, tx):
        """Pick something for the Expense.vendor field — best-effort."""
        # If the destination is a vendor, name them. Otherwise use the
        # USA bank or PK bank label so the Expense list is searchable.
        if tx.dest_vendor_id:
            return tx.dest_vendor.name
        if tx.dest_usa_bank_id:
            return tx.dest_usa_bank.label
        if tx.dest_pk_bank_id:
            return tx.dest_pk_bank.label
        return ""

    @dbtx.atomic

    def _sync_fee_distribution(self, tx, expense, custom_splits=None):
        """
        After creating/updating the fee expense, create distributions:
          - company: full fee → company slice
          - partner: full fee → single partner slice
          - custom:  use custom_splits list [{partner_id|None, amount}, ...]
                     If no custom_splits provided, keep existing distributions.
        """
        from myapp.Models.Expense_models import ExpenseDistribution
        from decimal import Decimal

        dist_type = getattr(tx, "fee_dist_type", "company") or "company"
        fee = Decimal(str(tx.fee_amount or "0"))
        if fee <= 0:
            expense.distributions.all().delete()
            return

        if dist_type == "custom":
            if not custom_splits:
                # No splits provided — leave existing distributions untouched
                return
            # Save custom splits
            expense.distributions.all().delete()
            for s in custom_splits:
                partner_id = s.get("partnerId") or s.get("partner_id") or None
                amount = Decimal(str(s.get("amount", 0)))
                if amount <= 0:
                    continue
                ExpenseDistribution.objects.create(
                    expense=expense,
                    partner_id=partner_id,
                    amount=amount,
                    updated_by=tx.created_by,
                )
            return

        # Clear and rebuild auto-distributions
        expense.distributions.all().delete()

        if dist_type == "partner" and tx.fee_dist_partner_id:
            ExpenseDistribution.objects.create(
                expense=expense,
                partner_id=tx.fee_dist_partner_id,
                amount=fee,
                updated_by=tx.created_by,
            )
        else:
            # company (default)
            ExpenseDistribution.objects.create(
                expense=expense,
                partner=None,
                amount=fee,
                updated_by=tx.created_by,
            )

    def _sync_fee_expense(self, tx, custom_splits=None):
        """Create / update / delete the linked Expense based on fee.

        Idempotent — safe to call after every save. Decisions:
          * CREDIT-CARD source → NEVER an expense. The bank fee on a card
            transaction is converted along with the spend: it's folded into
            `card_profit_pkr` = (amount + fee) × dollar rate by the
            serializer, which is PKR RECEIVED into our Pakistani banks (not
            profit). Any previously linked fee expense is removed so the
            overview / closing reports don't double-count it as a cost.
          * fee > 0  + linked expense missing → create
          * fee > 0  + linked expense exists  → update fields in place
          * fee == 0 + linked expense exists  → delete linked expense
          * fee == 0 + no linked expense      → no-op
        """
        from myapp.Models.InternalTx_models import InternalTxSource

        fee = Decimal(str(tx.fee_amount or "0"))
        fee_currency_code = (
            tx.fee_currency_id or tx.currency_id
        )

        # Card transactions: the fee converts into received PKR, not a
        # cost. Drop any linked expense (e.g. from before this rule, or
        # after the source_type was edited to credit_card) and bail out.
        if tx.source_type == InternalTxSource.CREDIT_CARD:
            if tx.fee_expense_id:
                old = tx.fee_expense
                tx.fee_expense = None
                tx.save(update_fields=["fee_expense", "updated_at"])
                if old is not None:
                    try:
                        old.delete()
                    except Exception:
                        pass
            return

        if fee <= 0:
            # Drop any stale linked expense so the dashboard doesn't
            # keep counting an outdated fee.
            if tx.fee_expense_id:
                old = tx.fee_expense
                tx.fee_expense = None
                tx.save(update_fields=["fee_expense", "updated_at"])
                if old is not None:
                    try:
                        old.delete()
                    except Exception:
                        # Already gone — nothing to do.
                        pass
            return

        title = self._fee_title(tx)
        vendor_name = self._fee_vendor_name(tx)
        purpose = (
            f"Auto-recorded fee for internal transaction "
            f"{tx.id} ({tx.amount} {tx.currency_id} "
            f"{tx.get_source_type_display()} → "
            f"{tx.get_destination_type_display()})."
        )

        if tx.fee_expense_id:
            exp = tx.fee_expense
            exp.title = title
            exp.category = ExpenseCategory.BANKING
            exp.vendor = vendor_name
            exp.currency_id = fee_currency_code
            exp.amount = fee
            exp.purpose = purpose
            exp.spent_on = tx.occurred_on
            exp.save()
            self._sync_fee_distribution(tx, exp, custom_splits=custom_splits)
            return

        exp = Expense.objects.create(
            title=title,
            category=ExpenseCategory.BANKING,
            vendor=vendor_name,
            currency_id=fee_currency_code,
            amount=fee,
            purpose=purpose,
            spent_on=tx.occurred_on,
            created_by=tx.created_by,
        )
        # Atomic link-back so we never lose track.
        tx.fee_expense = exp
        tx.save(update_fields=["fee_expense", "updated_at"])
        # Create expense distribution based on fee_dist_type
        self._sync_fee_distribution(tx, exp, custom_splits=custom_splits)

    def _sync_pk_fee_expense(self, tx):
        """Create / update / delete the linked PK-bank fee Expense.

        Mirrors `_sync_fee_expense` but for the Pakistani-bank fee
        (`pk_fee_amount`), kept as its own Expense row so each side's fee
        is independently auditable. The PK fee is always absorbed by the
        company (it's a banking cost on our incoming funds), so no
        partner-split logic is applied here.
        """
        pk_fee = Decimal(str(tx.pk_fee_amount or "0"))
        # PK fee is charged in `currency` (the USD we send); the bank
        # deducts it before converting. Record the expense in that currency.
        fee_currency_code = tx.currency_id

        if pk_fee <= 0:
            if tx.pk_fee_expense_id:
                old = tx.pk_fee_expense
                tx.pk_fee_expense = None
                tx.save(update_fields=["pk_fee_expense", "updated_at"])
                if old is not None:
                    try:
                        old.delete()
                    except Exception:
                        pass
            return

        dest = tx.destination_label() or "PK bank"
        title = f"PK bank fee — {dest}"[:200]
        vendor_name = (
            tx.dest_pk_bank.label if tx.dest_pk_bank_id else "PK bank"
        )
        purpose = (
            f"Auto-recorded Pakistani-bank fee ({tx.pk_fee_percent}%) for "
            f"internal transaction {tx.id} ({tx.amount} {tx.currency_id} "
            f"USA → PK bank)."
        )

        if tx.pk_fee_expense_id:
            exp = tx.pk_fee_expense
            exp.title = title
            exp.category = ExpenseCategory.BANKING
            exp.vendor = vendor_name
            exp.currency_id = fee_currency_code
            exp.amount = pk_fee
            exp.purpose = purpose
            exp.spent_on = tx.occurred_on
            exp.save()
            return

        exp = Expense.objects.create(
            title=title,
            category=ExpenseCategory.BANKING,
            vendor=vendor_name,
            currency_id=fee_currency_code,
            amount=pk_fee,
            purpose=purpose,
            spent_on=tx.occurred_on,
            created_by=tx.created_by,
        )
        tx.pk_fee_expense = exp
        tx.save(update_fields=["pk_fee_expense", "updated_at"])

    # ------------------------------------------------------------------
    # Auto-VendorPKRPayment sync
    # ------------------------------------------------------------------
    def _sync_vendor_pkr_payment(self, tx, pkr_received=None, screenshot=None, pk_bank_account_id=None, bank_transaction_id=None):
        """
        If the transaction has a pkr_converter_vendor set, auto-create or
        update a linked VendorPKRPayment so the "PKR Payments by Person"
        ledger stays in sync without manual double-entry.

        pkr_received: decimal/string PKR amount (explicit from the form).
                      Falls back to pk_amount_pkr if the transaction has it.
        screenshot:   uploaded image file, or None.
        """
        # A USD→PKR conversion only exists when the money left the US. A
        # USA→USA bank transfer never converts, so it must not carry a
        # converter or a linked payment — drop both if one was set before
        # the destination was changed.
        eligible = tx.destination_type in (
            InternalTxDestination.VENDOR, InternalTxDestination.PK_BANK,
        )
        if not eligible and tx.pkr_converter_vendor_id:
            tx.pkr_converter_vendor = None
            tx.save(update_fields=["pkr_converter_vendor", "updated_at"])

        if not eligible or not tx.pkr_converter_vendor_id:
            # No converter (or not eligible for one) — unlink, but keep the
            # historical VendorPKRPayment row.
            if tx.linked_vendor_pkr_payment_id:
                tx.linked_vendor_pkr_payment = None
                tx.save(update_fields=["linked_vendor_pkr_payment", "updated_at"])
            return

        vendor = tx.pkr_converter_vendor
        # Ensure the vendor is flagged for PKR conversion.
        if not vendor.handles_pkr_conversion:
            vendor.handles_pkr_conversion = True
            vendor.save(update_fields=["handles_pkr_conversion", "updated_at"])

        from decimal import Decimal as D
        usd_sent = D(str(tx.amount or 0))
        pkr_val = D(str(pkr_received or tx.pk_amount_pkr or 0))
        rate = tx.pk_conversion_rate or None

        # Resolve Pakistani bank account where transferred
        resolved_pk_bank_id = pk_bank_account_id or (tx.dest_pk_bank_id if tx.dest_pk_bank_id else None)

        common_fields = dict(
            vendor=vendor,
            usd_sent=usd_sent,
            pkr_received=pkr_val if pkr_val > 0 else usd_sent,
            exchange_rate=rate if rate else None,
            pk_bank_account_id=resolved_pk_bank_id,
            bank_transaction_id=bank_transaction_id or "",
            occurred_on=tx.occurred_on,
        )

        if tx.linked_vendor_pkr_payment_id:
            pmt = tx.linked_vendor_pkr_payment
            for k, v in common_fields.items():
                setattr(pmt, k, v)
            if screenshot:
                pmt.screenshot = screenshot
            pmt.save()
        else:
            pmt = VendorPKRPayment(
                **common_fields,
                created_by=tx.created_by,
            )
            if screenshot:
                pmt.screenshot = screenshot
            pmt.save()
            tx.linked_vendor_pkr_payment = pmt
            tx.save(update_fields=["linked_vendor_pkr_payment", "updated_at"])

    # ------------------------------------------------------------------
    # CRUD lifecycle hooks
    # ------------------------------------------------------------------
    def perform_create(self, serializer):
        obj = serializer.save(created_by=self.request.user)
        # Parse custom splits from request data if present
        raw_splits = self.request.data.get("fee_custom_splits")
        custom_splits = None
        if raw_splits:
            try:
                import json
                custom_splits = json.loads(raw_splits)
            except Exception:
                custom_splits = None
        self._sync_fee_expense(obj, custom_splits=custom_splits)
        self._sync_pk_fee_expense(obj)
        # Auto-create VendorPKRPayment if a converter vendor is attached.
        pkr_received = self.request.data.get("pkr_converter_pkr_received") or None
        screenshot = self.request.data.get("pkr_converter_screenshot") or None
        pk_bank_account_id = self.request.data.get("pkr_converter_pk_bank_account") or None
        bank_transaction_id = self.request.data.get("pkr_converter_bank_transaction_id") or None
        self._sync_vendor_pkr_payment(
            obj,
            pkr_received=pkr_received,
            screenshot=screenshot,
            pk_bank_account_id=pk_bank_account_id,
            bank_transaction_id=bank_transaction_id
        )
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=(
                f"Internal transaction created: "
                f"{obj.amount} {obj.currency_id} "
                f"{obj.get_source_type_display()} → "
                f"{obj.get_destination_type_display()}"
            ),
            metadata={
                "amount": str(obj.amount),
                "currency": obj.currency_id,
                "fee_amount": str(obj.fee_amount or 0),
                "method": obj.method,
            },
        )

    def perform_update(self, serializer):
        before = {
            "amount": str(serializer.instance.amount),
            "fee_amount": str(serializer.instance.fee_amount),
            "method": serializer.instance.method,
            "currency": serializer.instance.currency_id,
        }
        obj = serializer.save()
        raw_splits = self.request.data.get("fee_custom_splits")
        custom_splits = None
        if raw_splits:
            try:
                import json
                custom_splits = json.loads(raw_splits)
            except Exception:
                custom_splits = None
        self._sync_fee_expense(obj, custom_splits=custom_splits)
        self._sync_pk_fee_expense(obj)
        # Sync the linked VendorPKRPayment if vendor set.
        pkr_received = self.request.data.get("pkr_converter_pkr_received") or None
        screenshot = self.request.data.get("pkr_converter_screenshot") or None
        pk_bank_account_id = self.request.data.get("pkr_converter_pk_bank_account") or None
        bank_transaction_id = self.request.data.get("pkr_converter_bank_transaction_id") or None
        self._sync_vendor_pkr_payment(
            obj,
            pkr_received=pkr_received,
            screenshot=screenshot,
            pk_bank_account_id=pk_bank_account_id,
            bank_transaction_id=bank_transaction_id
        )
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=(
                f"Internal transaction updated: "
                f"{obj.amount} {obj.currency_id}"
            ),
            metadata={
                "before": before,
                "after": {
                    "amount": str(obj.amount),
                    "fee_amount": str(obj.fee_amount or 0),
                    "method": obj.method,
                    "currency": obj.currency_id,
                },
            },
        )

    @dbtx.atomic
    def perform_destroy(self, instance):
        # Remove the linked fee expense alongside the transaction so
        # we don't leave an orphaned expense row behind.
        snapshot = {
            "id": str(instance.id),
            "amount": str(instance.amount),
            "fee_amount": str(instance.fee_amount or 0),
            "currency": instance.currency_id,
            "method": instance.method,
            "source_type": instance.source_type,
            "destination_type": instance.destination_type,
        }
        linked = instance.fee_expense
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_DELETE,
            target=instance,
            description=f"Internal transaction deleted: {instance}",
            metadata=snapshot,
        )
        instance.delete()
        if linked is not None:
            try:
                linked.delete()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Aggregates for the dashboard band on the page
    # ------------------------------------------------------------------
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Per-currency totals across the filtered queryset.

        Mirrors the Expenses page's `summary` action so the UI can
        render the same dashboard band. Counts and totals respect
        the same filters (date range, method, currency, etc.).
        """
        qs = self.filter_queryset(self.get_queryset()).order_by()

        by_currency = list(
            qs.values("currency_id")
              .annotate(
                  total=Sum("amount"),
                  fees=Sum("fee_amount"),
                  pk_fees=Sum("pk_fee_amount"),
                  count=Count("id"),
              )
              .order_by("currency_id")
        )

        by_destination = list(
            qs.values("destination_type")
              .annotate(
                  count=Count("id"),
                  total_pkr=Sum(
                      "amount", filter=Q(currency_id="PKR"),
                  ),
              )
              .order_by("destination_type")
        )

        # PKR that landed in our Pakistani banks via USA→PK BANK transfers
        # (sum of the computed `pk_amount_pkr`).
        bank_received_pkr = (
            qs.filter(destination_type="pk_bank")
              .aggregate(v=Sum("pk_amount_pkr"))["v"]
            or 0
        )

        # PKR that landed in our Pakistani banks via CARD transactions.
        #
        # This is (amount + fee_amount) × card_dollar_rate — stored in the
        # legacy `card_profit_pkr` column. Despite the column name it is NOT
        # company profit: it is rupees received into the PK banks, exactly
        # like a USA→PK bank transfer. It funds customer payouts and must
        # therefore sit in the reconciliation pool, never in the profit
        # figure. (The column is left named `card_profit_pkr` to avoid a
        # data migration; the API exposes it as `card_received_pkr`.)
        card_received_pkr = (
            qs.filter(source_type="credit_card")
              .aggregate(v=Sum("card_profit_pkr"))["v"]
            or 0
        )

        # PKR received directly from enabled people/vendors in exchange for
        # USD sent to them. Match the same date window used by this summary.
        converter_qs = VendorPKRPayment.objects.all()
        if request.query_params.get("date_from"):
            converter_qs = converter_qs.filter(
                occurred_on__gte=request.query_params["date_from"],
            )
        if request.query_params.get("date_to"):
            converter_qs = converter_qs.filter(
                occurred_on__lte=request.query_params["date_to"],
            )
        converter_received_pkr = (
            converter_qs.aggregate(v=Sum("pkr_received"))["v"] or 0
        )

        # The single rupee pool = bank transfers + card transactions +
        # direct PKR converter receipts.
        pk_received_pkr = (
            bank_received_pkr + card_received_pkr + converter_received_pkr
        )

        return Response({
            "total_count": qs.count(),
            "pk_received_pkr": str(pk_received_pkr),
            "bank_received_pkr": str(bank_received_pkr),
            "card_received_pkr": str(card_received_pkr),
            "converter_received_pkr": str(converter_received_pkr),
            # Back-compat alias. Always equals card_received_pkr; it is NOT
            # profit. Kept so older clients don't KeyError. Do not add to
            # any profit total.
            "card_profit_pkr": str(card_received_pkr),
            "by_currency": [
                {
                    "currency": r["currency_id"],
                    "total": str(r["total"] or 0),
                    "fees": str(r["fees"] or 0),
                    "pk_fees": str(r["pk_fees"] or 0),
                    "count": r["count"],
                }
                for r in by_currency
            ],
            "by_destination": [
                {
                    "destination_type": r["destination_type"],
                    "count": r["count"],
                    "total_pkr": str(r["total_pkr"] or 0),
                }
                for r in by_destination
            ],
        })
