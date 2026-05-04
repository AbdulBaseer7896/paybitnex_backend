"""
Dispatch views — DispatchCompany, DispatchDriver, Dispatch CRUD plus
the "generate invoice for dispatch fee" action.

All endpoints are scoped to the authenticated customer; admin/accountant
users get an empty queryset by design (the data is private to the customer).

Gated behind the `dispatch` premium feature — customers without the
grant get 403. Staff (admin/accountant) bypass the gate but still see
no data.
"""
from datetime import timedelta
from decimal import Decimal
import secrets
import logging

from django.db import transaction as dbtx
from django.db.models import Q, Count, Sum
from django.utils import timezone

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response

from myapp.Models.Auth_models import UserRole
from myapp.Models.Audit_models import AuditLog
from myapp.Models.Dispatch_models import (
    DispatchCompany, DispatchDriver, Dispatch, DispatchStatus,
)
from myapp.Models.Invoicing_models import (
    Client, CustomerCompany, Invoice, InvoiceLineItem,
    InvoiceStatus, InvoicePaymentMethod, CustomerAllowedPaymentMethod,
)
from myapp.Models.Core_models import PaymentMethod

from myapp.serializers.Dispatch_serializers import (
    DispatchCompanySerializer, DispatchDriverSerializer,
    DispatchListSerializer, DispatchDetailSerializer,
    DispatchToInvoiceSerializer,
)
from myapp.Utils.permissions import HasFeature
from myapp.Utils.features import user_has_feature

log = logging.getLogger(__name__)


def _is_customer(request):
    return getattr(request.user, "role", None) == UserRole.CUSTOMER


# ─────────────────────────── Companies ──────────────────────────────


class DispatchCompanyViewSet(viewsets.ModelViewSet):
    """
    Customer-owned trucking/carrier companies.

    Endpoints:
      GET    /dispatch/companies/                 List my companies
      POST   /dispatch/companies/                 Create
      GET    /dispatch/companies/<uuid>/          Detail
      PUT    /dispatch/companies/<uuid>/          Update
      DELETE /dispatch/companies/<uuid>/          Archive (soft-delete)
      POST   /dispatch/companies/<uuid>/restore/  Un-archive
    """
    permission_classes = [IsAuthenticated, HasFeature("dispatch")]
    serializer_class = DispatchCompanySerializer

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return DispatchCompany.objects.none()
        qs = DispatchCompany.objects.filter(customer=u)
        p = self.request.query_params
        if p.get("include_archived") not in ("1", "true", "True", "yes"):
            qs = qs.exclude(is_archived=True)
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(mc_number__icontains=search)
                | Q(contact_name__icontains=search)
                | Q(contact_email__icontains=search),
            )
        # Annotate counts so the list view doesn't N+1.
        qs = qs.annotate(
            _drivers_count=Count(
                "drivers",
                filter=Q(drivers__is_archived=False),
                distinct=True,
            ),
            _loads_count=Count("dispatches", distinct=True),
        )
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        obj = serializer.save(customer=self.request.user)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"Dispatch company created: {obj.name}",
        )

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Dispatch company updated: {obj.name}",
        )

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        # Soft-delete via is_archived so historical loads keep their FK.
        obj.is_archived = True
        obj.save(update_fields=["is_archived", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Dispatch company archived: {obj.name}",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        obj = self.get_object()
        obj.is_archived = False
        obj.save(update_fields=["is_archived", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Dispatch company restored: {obj.name}",
        )
        return Response(self.get_serializer(obj).data)


# ─────────────────────────── Drivers ────────────────────────────────


class DispatchDriverViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasFeature("dispatch")]
    serializer_class = DispatchDriverSerializer

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return DispatchDriver.objects.none()
        qs = DispatchDriver.objects.filter(customer=u).select_related("company")
        p = self.request.query_params
        if p.get("include_archived") not in ("1", "true", "True", "yes"):
            qs = qs.exclude(is_archived=True)
        if p.get("company"):
            qs = qs.filter(company_id=p.get("company"))
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(email__icontains=search)
                | Q(license_number__icontains=search)
                | Q(company__name__icontains=search),
            )
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        obj = serializer.save(customer=self.request.user)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"Driver created: {obj.name} @ {obj.company.name}",
        )

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Driver updated: {obj.name}",
        )

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        obj.is_archived = True
        obj.save(update_fields=["is_archived", "updated_at"])
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Driver archived: {obj.name}",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        obj = self.get_object()
        obj.is_archived = False
        obj.save(update_fields=["is_archived", "updated_at"])
        return Response(self.get_serializer(obj).data)


# ─────────────────────────── Dispatches ─────────────────────────────


class DispatchViewSet(viewsets.ModelViewSet):
    """
    The actual dispatched loads.

    Endpoints:
      GET    /dispatch/loads/                List + filters + summary
      POST   /dispatch/loads/                Create
      GET    /dispatch/loads/<uuid>/         Detail
      PUT    /dispatch/loads/<uuid>/         Update
      DELETE /dispatch/loads/<uuid>/         Hard-delete (only allowed if
                                              not invoiced)
      POST   /dispatch/loads/<uuid>/mark-paid/   Flip is_paid + status
      POST   /dispatch/loads/<uuid>/mark-unpaid/ Reverse mark-paid
      POST   /dispatch/loads/<uuid>/generate-invoice/  Build an invoice
                                              for the dispatch fee. Body
                                              picks the client/company/
                                              payment method.
      GET    /dispatch/loads/summary/        Aggregate stats for the
                                              dashboard widget.
    """
    permission_classes = [IsAuthenticated, HasFeature("dispatch")]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_serializer_class(self):
        if self.action == "list":
            return DispatchListSerializer
        return DispatchDetailSerializer

    def get_queryset(self):
        u = self.request.user
        if not _is_customer(self.request):
            return Dispatch.objects.none()
        qs = (Dispatch.objects.filter(customer=u)
              .select_related("company", "driver", "invoice"))
        p = self.request.query_params
        if p.get("company"):
            qs = qs.filter(company_id=p.get("company"))
        if p.get("driver"):
            qs = qs.filter(driver_id=p.get("driver"))
        if p.get("status"):
            qs = qs.filter(status=p.get("status"))
        if p.get("is_paid") in ("1", "true", "True"):
            qs = qs.filter(is_paid=True)
        elif p.get("is_paid") in ("0", "false", "False"):
            qs = qs.filter(is_paid=False)
        if p.get("date_from"):
            qs = qs.filter(pickup_date__gte=p.get("date_from"))
        if p.get("date_to"):
            qs = qs.filter(pickup_date__lte=p.get("date_to"))
        if p.get("invoiced") in ("1", "true", "True"):
            qs = qs.filter(invoice__isnull=False)
        elif p.get("invoiced") in ("0", "false", "False"):
            qs = qs.filter(invoice__isnull=True)
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(load_number__icontains=search)
                | Q(broker_name__icontains=search)
                | Q(pickup_location__icontains=search)
                | Q(dropoff_location__icontains=search)
                | Q(dispatcher_name__icontains=search)
                | Q(company__name__icontains=search)
                | Q(driver__name__icontains=search),
            )
        return qs.order_by("-pickup_date", "-created_at")

    def perform_create(self, serializer):
        obj = serializer.save(customer=self.request.user)
        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_CREATE,
            target=obj,
            description=f"Dispatch created: load #{obj.load_number or obj.id}",
        )

    # ── Invoice lock ──────────────────────────────────────────────────
    # Once a load has been billed (invoice FK is set) the operational
    # data on the load is frozen — pickup, dropoff, rate, fee, etc. all
    # need to match what was sent to the client. Only `status` may be
    # updated after that point (for in-transit → delivered → paid
    # progression tracking). Trying to PATCH any locked field returns
    # a 400 with a clear message; the frontend hides the Edit button
    # accordingly to avoid surprising the customer.
    LOCKED_FIELDS_AFTER_INVOICE = {
        "company", "driver", "truck_type",
        "broker_name", "broker_phone", "broker_email", "broker_mc",
        "load_number",
        "booked_date", "pickup_date", "delivery_date",
        "pickup_location", "dropoff_location", "extra_stops",
        "loaded_miles", "deadhead_miles",
        "rate", "dispatch_fee_percent", "dispatch_fee_flat",
        "dispatcher_name",
        "rate_confirmation",
    }

    def _enforce_invoice_lock(self, request):
        """Return a 400 Response if request.data contains locked fields
        for an already-invoiced dispatch. Otherwise None."""
        instance = self.get_object()
        if not instance.invoice_id:
            return None
        attempted = set(request.data.keys()) & self.LOCKED_FIELDS_AFTER_INVOICE
        if not attempted:
            return None
        return Response(
            {
                "detail": (
                    "This load has already been invoiced. Only the "
                    "status can be updated. Void the invoice first if "
                    "you need to edit other fields."
                ),
                "locked_fields": sorted(attempted),
                "invoice_id": str(instance.invoice_id),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    def update(self, request, *args, **kwargs):
        guard = self._enforce_invoice_lock(request)
        if guard is not None:
            return guard
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        guard = self._enforce_invoice_lock(request)
        if guard is not None:
            return guard
        return super().partial_update(request, *args, **kwargs)

    def perform_update(self, serializer):
        # Capture the prior status from the DB before save so we can
        # decide whether the customer just transitioned to/from "paid"
        # via the inline status picker on the loads list. We mirror
        # that change to is_paid + paid_at so all three fields stay
        # consistent regardless of which control was used.
        # `serializer.instance` here is still the unsaved-but-modified
        # object — its `.status` field already reflects the request
        # body, so we can't read prev from it. Re-fetch a clean copy.
        prev_status = None
        if serializer.instance and serializer.instance.pk:
            prev_status = (
                Dispatch.objects.filter(pk=serializer.instance.pk)
                .values_list("status", flat=True)
                .first()
            )

        obj = serializer.save()
        new_status = obj.status

        if prev_status is not None and prev_status != new_status:
            if new_status == DispatchStatus.PAID and not obj.is_paid:
                obj.is_paid = True
                obj.paid_at = timezone.now()
                obj.save(update_fields=["is_paid", "paid_at", "updated_at"])
            elif prev_status == DispatchStatus.PAID and obj.is_paid:
                # Moved off "paid" → un-flag.
                obj.is_paid = False
                obj.paid_at = None
                obj.save(update_fields=["is_paid", "paid_at", "updated_at"])

        AuditLog.record(
            user=self.request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Dispatch updated: load #{obj.load_number or obj.id}",
        )

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.invoice_id:
            return Response(
                {"detail": "Cannot delete a dispatch that has an invoice. "
                           "Void the invoice first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_DELETE,
            target=obj,
            description=f"Dispatch deleted: load #{obj.load_number or obj.id}",
        )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        obj = self.get_object()
        obj.is_paid = True
        obj.paid_at = timezone.now()
        if obj.status == DispatchStatus.DELIVERED:
            obj.status = DispatchStatus.PAID
        obj.save()
        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_UPDATE,
            target=obj,
            description=f"Dispatch marked paid: load #{obj.load_number or obj.id}",
        )
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"], url_path="mark-unpaid")
    def mark_unpaid(self, request, pk=None):
        obj = self.get_object()
        obj.is_paid = False
        obj.paid_at = None
        if obj.status == DispatchStatus.PAID:
            obj.status = DispatchStatus.DELIVERED
        obj.save()
        return Response(self.get_serializer(obj).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Aggregate widget — totals at the top of the dispatch list."""
        if not _is_customer(request):
            return Response({})
        qs = self.get_queryset()
        totals = qs.aggregate(
            total_loads=Count("id"),
            total_rate=Sum("rate"),
            total_dispatch_fee=Sum("dispatch_fee"),
            total_miles=Sum("loaded_miles"),
        )
        paid = qs.filter(is_paid=True).aggregate(
            paid_count=Count("id"),
            paid_dispatch_fee=Sum("dispatch_fee"),
        )
        unpaid = qs.filter(is_paid=False).aggregate(
            unpaid_count=Count("id"),
            unpaid_dispatch_fee=Sum("dispatch_fee"),
        )
        return Response({
            "total_loads": totals["total_loads"] or 0,
            "total_rate": str(totals["total_rate"] or Decimal("0")),
            "total_dispatch_fee": str(totals["total_dispatch_fee"] or Decimal("0")),
            "total_miles": totals["total_miles"] or 0,
            "paid_count": paid["paid_count"] or 0,
            "paid_dispatch_fee": str(paid["paid_dispatch_fee"] or Decimal("0")),
            "unpaid_count": unpaid["unpaid_count"] or 0,
            "unpaid_dispatch_fee": str(unpaid["unpaid_dispatch_fee"] or Decimal("0")),
        })

    @action(detail=True, methods=["post"], url_path="generate-invoice")
    @dbtx.atomic
    def generate_invoice(self, request, pk=None):
        """Build an invoice for this dispatch's dispatch fee.

        The customer picks a Client (the broker / billing entity) and
        one of their CustomerCompany letterheads; we create a single-
        line invoice with the dispatch fee as the amount and link it
        back to the dispatch.

        Requires the `invoicing` feature in addition to `dispatch` so
        we can actually create the invoice. If the customer doesn't
        have invoicing enabled we return 403 with a clear message.
        """
        # Additional feature gate — dispatch alone isn't enough; we
        # need invoicing too because we're literally creating an invoice.
        if not user_has_feature(request.user, "invoicing"):
            return Response(
                {"detail": "Invoicing feature is required to generate "
                           "invoices from dispatches. Contact your "
                           "administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )

        dispatch = self.get_object()

        if dispatch.invoice_id:
            return Response(
                {"detail": "An invoice has already been generated for "
                           "this dispatch.",
                 "invoice_id": str(dispatch.invoice_id)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        s = DispatchToInvoiceSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        # Resolve the picked client + company, scoped to the customer.
        try:
            client = Client.objects.get(
                id=d["client_id"], customer=request.user,
            )
        except Client.DoesNotExist:
            return Response(
                {"detail": "Client not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            company = CustomerCompany.objects.get(
                id=d["company_id"], customer=request.user,
            )
        except CustomerCompany.DoesNotExist:
            return Response(
                {"detail": "Company not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve payment methods — same logic as InvoiceViewSet.create.
        # We import the helpers from Invoicing_views lazily to avoid
        # circular imports.
        from myapp.Views.Invoicing_views import (
            _generate_invoice_number, _compute_totals,
            _snapshot_client, _snapshot_company, _snapshot_payment_method,
            _resolve_payment_method_for_invoice, _build_and_cache_pdf,
        )

        requested_codes = d.get("payment_method_codes") or []
        allowed_ids = set(CustomerAllowedPaymentMethod.objects.filter(
            customer=request.user,
            payment_method__is_active=True,
        ).values_list("payment_method_id", flat=True))
        chosen_methods = [
            pm for pm in PaymentMethod.objects.filter(
                code__in=requested_codes,
                is_active=True,
            )
            if pm.code in allowed_ids
        ]
        chosen_methods.sort(key=lambda pm: requested_codes.index(pm.code))

        if not chosen_methods:
            fallback = _resolve_payment_method_for_invoice(
                request.user, None,
            )
            if fallback:
                chosen_methods = [fallback]

        primary_method = chosen_methods[0] if chosen_methods else None

        # Build a one-line item describing the dispatch fee.
        line_items = [{
            "position": 0,
            "name": (
                f"Dispatch fee — Load #{dispatch.load_number or dispatch.id}"
                if dispatch.load_number
                else f"Dispatch fee — {dispatch.pickup_location} → "
                     f"{dispatch.dropoff_location}"
            ),
            "description": _build_invoice_line_description(dispatch),
            "quantity": Decimal("1"),
            "unit_price": dispatch.dispatch_fee or Decimal("0"),
        }]

        tax_percent = d.get("tax_percent") or Decimal("0")
        subtotal, tax_amt, total = _compute_totals(line_items, tax_percent)

        number = _generate_invoice_number(company)
        expiry_days = company.invoice_link_expiry_days
        expires_at = (timezone.now() + timedelta(days=expiry_days)
                      if expiry_days else None)

        # Default the due date to 14 days from today when blank —
        # same Net-14 convention as the regular invoice flow. Keeps
        # generated invoices from going out with no deadline.
        due_date_value = d.get("due_date")
        if not due_date_value:
            due_date_value = timezone.now().date() + timedelta(days=14)

        invoice = Invoice.objects.create(
            customer=request.user,
            client=client,
            company=company,
            payment_method=primary_method,
            number=number,
            currency_code="USD",
            subtotal=subtotal,
            tax_percent=tax_percent,
            tax_amount=tax_amt,
            total=total,
            due_date=due_date_value,
            general_description=(
                f"Dispatch fee for load #{dispatch.load_number or dispatch.id}"
            ),
            notes=d.get("notes", ""),
            status=InvoiceStatus.DRAFT,
            share_token=secrets.token_urlsafe(32),
            expires_at=expires_at,
            client_snapshot=_snapshot_client(client),
            company_snapshot=_snapshot_company(company, request),
            payment_method_snapshot=_snapshot_payment_method(
                primary_method, request,
            ),
        )

        InvoiceLineItem.objects.create(
            invoice=invoice,
            position=0,
            name=line_items[0]["name"],
            description=line_items[0]["description"],
            quantity=line_items[0]["quantity"],
            unit_price=line_items[0]["unit_price"],
        )

        for idx, pm in enumerate(chosen_methods):
            InvoicePaymentMethod.objects.create(
                invoice=invoice,
                payment_method=pm,
                position=idx,
                snapshot=_snapshot_payment_method(pm, request),
            )

        # Link back from the dispatch.
        dispatch.invoice = invoice
        dispatch.save(update_fields=["invoice", "updated_at"])

        # Build PDF (best-effort; non-fatal).
        try:
            _build_and_cache_pdf(invoice)
        except Exception as e:
            log.warning("dispatch invoice pdf generation failed: %s", e)

        AuditLog.record(
            user=request.user, action=AuditLog.ACTION_CREATE, target=invoice,
            description=f"Invoice {invoice.number} generated from dispatch "
                        f"#{dispatch.load_number or dispatch.id}",
        )

        # ── Action: 'draft' (default) keeps it as a draft, 'send'
        # immediately emails the client + customer just like the
        # standard InvoiceViewSet.create flow does. We piggyback on
        # InvoiceViewSet._send_emails so the email templates and
        # status transitions stay consistent across both creation
        # paths.
        action_flag = (request.data.get("action") or "draft").lower()
        sent_count = 0
        if action_flag == "send":
            try:
                from myapp.Views.Invoicing_views import InvoiceViewSet
                # Instantiate just enough of the viewset to call
                # _send_emails. It only needs `request` for absolute-
                # URL building inside the email helpers.
                invs = InvoiceViewSet()
                invs.request = request
                sent = invs._send_emails(invoice) or []
                sent_count = len(sent)
            except Exception as e:
                # Non-fatal — invoice was already created. Log and
                # report back so the frontend can show a partial-
                # success toast.
                log.warning("dispatch-invoice send failed: %s", e)

        return Response(
            {
                "dispatch": DispatchDetailSerializer(
                    dispatch, context={"request": request},
                ).data,
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.number,
                "action": action_flag,
                "emails_sent": sent_count,
            },
            status=status.HTTP_201_CREATED,
        )


def _build_invoice_line_description(dispatch):
    """Compose a useful description string for the invoice line item.

    Pulls together the dispatch fields that a billing recipient would
    care about — pickup, dropoff, dates, miles, broker, dispatcher.
    Keeps the formatting plain so it survives PDF rendering across
    themes.
    """
    parts = []
    if dispatch.pickup_date:
        parts.append(f"Pickup: {dispatch.pickup_date}")
    if dispatch.delivery_date:
        parts.append(f"Delivery: {dispatch.delivery_date}")
    if dispatch.pickup_location and dispatch.dropoff_location:
        parts.append(f"{dispatch.pickup_location} → {dispatch.dropoff_location}")
    if dispatch.loaded_miles:
        parts.append(f"{dispatch.loaded_miles} miles")
    if dispatch.broker_name:
        parts.append(f"Broker: {dispatch.broker_name}")
    if dispatch.dispatcher_name:
        parts.append(f"Dispatcher: {dispatch.dispatcher_name}")
    return " · ".join(parts)
