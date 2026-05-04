"""
Dispatch module — customer-owned dispatch companies, drivers, and loads.

A customer who books loads for trucking companies stores three kinds of
records here:

- `DispatchCompany`  — a trucking company (carrier) the customer dispatches
                       for. The customer maintains a roster of these in
                       Account Settings → My Trucking Companies.
- `DispatchDriver`   — a driver belonging to one of those companies. The
                       customer maintains drivers in the same settings tab,
                       grouped under their company.
- `Dispatch`         — an actual dispatched load with all the load details
                       (pickup, dropoff, broker, rate, dispatch fee, etc.).
                       Closely modeled on the spreadsheet columns the
                       customer already uses.

The dispatch feature is gated behind the `dispatch` premium feature flag,
mirroring the pattern used for `invoicing`. Admin-side CRUD for these
tables is intentionally not exposed (per spec the data is owned by the
customer); audit logs cover dispute resolution.

Each Dispatch can optionally be linked to an Invoice generated from it
(for the dispatch fee billing). That link is non-destructive — voiding an
invoice does not delete the dispatch.
"""
import uuid
from decimal import Decimal

from django.db import models


class DispatchCompany(models.Model):
    """A trucking/carrier company the customer dispatches loads for.

    Scoped to a single customer. Drivers belong to a company; loads
    reference both. Soft-deletes via `is_archived` so historical loads
    keep their company name even after the customer stops using it.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="dispatch_companies",
    )

    name = models.CharField(max_length=200)
    mc_number = models.CharField(
        max_length=64, blank=True, default="",
        help_text="MC / DOT identifier for the carrier (optional).",
    )
    contact_name = models.CharField(max_length=150, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=32, blank=True, default="")
    address = models.TextField(blank=True, default="")

    # Default dispatch fee percentage for this company. Used as a
    # convenience when creating new loads — the customer can override
    # it per load. Stored as a percentage (e.g. 5.00 means 5%).
    default_dispatch_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("5.00"),
        help_text="Default dispatch fee % charged on this company's loads.",
    )

    notes = models.TextField(
        blank=True, default="",
        help_text="Private notes the customer keeps about this trucking "
                  "company. Not shown on any invoice.",
    )

    is_archived = models.BooleanField(
        default=False,
        help_text="Soft-delete flag. Archived companies stay linked to "
                  "existing loads but disappear from the picker.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatch_companies"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "is_archived"]),
            models.Index(fields=["customer", "name"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.customer_id})"


class DispatchDriver(models.Model):
    """A driver employed by one of the customer's trucking companies.

    Scoped to a single customer; soft-FK'd to a DispatchCompany. A
    driver's truck info (truck type) is stored here so it auto-fills
    when creating a load — but each load also stores a `truck_type`
    snapshot so changing the driver's truck type later doesn't
    retroactively change historical loads.
    """
    TRUCK_TYPES = [
        ("dry_van", "Dry Van"),
        ("reefer", "Reefer"),
        ("flatbed", "Flatbed"),
        ("step_deck", "Step Deck"),
        ("box_truck", "Box Truck"),
        ("power_only", "Power Only"),
        ("hotshot", "Hotshot"),
        ("conestoga", "Conestoga"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="dispatch_drivers",
    )
    company = models.ForeignKey(
        "myapp.DispatchCompany", on_delete=models.PROTECT,
        related_name="drivers",
    )

    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    license_number = models.CharField(max_length=64, blank=True, default="")

    truck_type = models.CharField(
        max_length=32, choices=TRUCK_TYPES,
        blank=True, default="",
        help_text="Default truck type — auto-fills the load form.",
    )
    truck_number = models.CharField(max_length=64, blank=True, default="")
    trailer_number = models.CharField(max_length=64, blank=True, default="")

    notes = models.TextField(blank=True, default="")

    is_archived = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatch_drivers"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["customer", "is_archived"]),
            models.Index(fields=["customer", "company"]),
            models.Index(fields=["company", "is_archived"]),
        ]

    def __str__(self):
        return f"{self.name} @ {self.company_id}"


class DispatchStatus:
    BOOKED = "booked"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    PAID = "paid"
    CANCELLED = "cancelled"

    CHOICES = [
        (BOOKED, "Booked"),
        (IN_TRANSIT, "In Transit"),
        (DELIVERED, "Delivered"),
        (PAID, "Paid"),
        (CANCELLED, "Cancelled"),
    ]


class Dispatch(models.Model):
    """A single dispatched load.

    Models the row-by-row spreadsheet structure the customer already
    uses. Each load belongs to a company and a driver and has a
    pickup/dropoff pair, broker information, mileage, rate, and a
    dispatch fee (the customer's commission for booking the load).

    Optional fields like `load_number`, `dispatcher`, and `notes` line
    up with the columns we saw in the customer's existing spreadsheet
    so importing historical data later is straightforward.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Owner ──
    customer = models.ForeignKey(
        "myapp.User", on_delete=models.CASCADE,
        related_name="dispatches",
    )

    # ── Carrier + driver (FK + snapshot to survive renames/archives) ──
    company = models.ForeignKey(
        "myapp.DispatchCompany", on_delete=models.PROTECT,
        related_name="dispatches",
    )
    driver = models.ForeignKey(
        "myapp.DispatchDriver", on_delete=models.PROTECT,
        related_name="dispatches",
        null=True, blank=True,
    )
    company_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    driver_name_snapshot = models.CharField(max_length=150, blank=True, default="")

    truck_type = models.CharField(
        max_length=32, choices=DispatchDriver.TRUCK_TYPES,
        blank=True, default="",
    )

    # ── Broker / shipper info ──
    broker_name = models.CharField(max_length=200, blank=True, default="")
    broker_phone = models.CharField(max_length=64, blank=True, default="")
    broker_email = models.EmailField(blank=True, default="")
    broker_mc = models.CharField(max_length=64, blank=True, default="")

    # ── Load details ──
    load_number = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Load # assigned by the broker / shipper.",
    )
    booked_date = models.DateField(null=True, blank=True)
    pickup_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)

    pickup_location = models.CharField(max_length=255, blank=True, default="")
    dropoff_location = models.CharField(max_length=255, blank=True, default="")
    # Stored as text because customer pastes multi-stop strings like
    # "Cedar Falls, IA - Portage, WI - Wauwatosa, WI" verbatim.
    extra_stops = models.TextField(
        blank=True, default="",
        help_text="Extra stops between pickup and final dropoff.",
    )

    loaded_miles = models.PositiveIntegerField(default=0)
    deadhead_miles = models.PositiveIntegerField(default=0)

    # ── Money ──
    rate = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        help_text="Total rate paid by the broker for the load.",
    )
    # The dispatch fee can be specified as either a percent OR a flat
    # amount; if a flat amount is provided it wins. Both stored so the
    # original intent is preserved across edits.
    dispatch_fee_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        help_text="Dispatcher's % of the load rate.",
    )
    dispatch_fee_flat = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        help_text="Optional flat dispatch fee. If non-zero, overrides the percent.",
    )
    # Cached resolved fee (= flat if non-zero, else percent of rate).
    dispatch_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
    )

    # ── People ──
    dispatcher_name = models.CharField(
        max_length=150, blank=True, default="",
        help_text="Internal dispatcher / agent who booked the load.",
    )

    # ── Workflow ──
    status = models.CharField(
        max_length=16, choices=DispatchStatus.CHOICES,
        default=DispatchStatus.BOOKED, db_index=True,
    )
    is_paid = models.BooleanField(
        default=False, db_index=True,
        help_text="Whether the broker has paid for the load.",
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    # ── Invoice link (optional) ──
    # When the customer generates an invoice for the dispatch fee from
    # this load, we keep a soft pointer so the dispatches list can show
    # invoice status and the invoice detail page can link back. Cleared
    # automatically by the SET_NULL on_delete if the invoice is hard-
    # deleted (rare; usually voided instead).
    invoice = models.ForeignKey(
        "myapp.Invoice", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="dispatches",
    )

    # ── Notes / attachments ──
    notes = models.TextField(blank=True, default="")
    rate_confirmation = models.FileField(
        upload_to="dispatches/rate_confirmation/", null=True, blank=True,
        help_text="Optional rate-confirmation PDF uploaded by the dispatcher.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatches"
        ordering = ["-pickup_date", "-created_at"]
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["customer", "-pickup_date"]),
            models.Index(fields=["customer", "is_paid"]),
            models.Index(fields=["company", "-pickup_date"]),
            models.Index(fields=["driver", "-pickup_date"]),
        ]

    def save(self, *args, **kwargs):
        # Keep snapshots in sync at write-time so even orphaned loads
        # (after archive/delete) display correctly in lists.
        if self.company_id and not self.company_name_snapshot:
            try:
                self.company_name_snapshot = self.company.name
            except Exception:
                pass
        if self.driver_id and not self.driver_name_snapshot:
            try:
                self.driver_name_snapshot = self.driver.name
            except Exception:
                pass

        # Resolve the dispatch fee. Flat wins when non-zero; otherwise
        # apply the percent against the rate.
        flat = self.dispatch_fee_flat or Decimal("0")
        if flat > 0:
            self.dispatch_fee = flat.quantize(Decimal("0.01"))
        else:
            pct = self.dispatch_fee_percent or Decimal("0")
            rate = self.rate or Decimal("0")
            self.dispatch_fee = ((rate * pct) / Decimal("100")).quantize(Decimal("0.01"))

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Dispatch {self.load_number or self.id} ({self.customer_id})"
