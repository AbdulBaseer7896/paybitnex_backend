"""
Auto-assignment of default payment methods to customers.

When a PaymentMethod has is_default=True, it is automatically assigned
to every customer (existing and new). If an admin explicitly removes it
for a specific customer, that exclusion is remembered via admin_excluded=True
and the method won't be re-assigned on future sync runs.

Key functions:
    assign_defaults_to_user(user)
        Called on new customer registration. Assigns all current
        is_default=True methods (unless they've been admin-excluded for
        this user, which can't happen on first registration, but keeps
        the function idempotent).

    sync_default_to_all_customers(payment_method, user=None)
        Called when admin marks a method as default (or removes default flag).
        - If is_default=True: assign to all customers who don't have it
          (skipping any who have admin_excluded=True for this method).
        - If is_default=False: do nothing — existing grants are not removed.

    handle_customer_removal(allowed_method_obj)
        Called when admin removes a CustomerAllowedPaymentMethod.
        If the grant was auto-assigned (auto_assigned=True), mark it as
        admin_excluded=True instead of deleting it, so we can skip it
        in future auto-assign runs.
        If manually granted, delete it normally.
"""
import logging
from django.db import transaction as dbtx

log = logging.getLogger(__name__)


def assign_defaults_to_user(user, granted_by=None):
    """
    Assign all is_default=True payment methods to a newly created customer.
    Safe to call multiple times (idempotent).
    """
    from myapp.Models.Core_models import PaymentMethod
    from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod

    if getattr(user, 'role', None) != 'customer':
        return  # Only customers get payment methods

    default_methods = PaymentMethod.objects.filter(is_default=True, is_active=True)
    if not default_methods.exists():
        return

    with dbtx.atomic():
        created_count = 0
        for method in default_methods:
            # Check if there's an existing grant or exclusion for this (user, method)
            existing = CustomerAllowedPaymentMethod.objects.filter(
                customer=user,
                payment_method=method,
            ).first()

            if existing:
                if existing.admin_excluded:
                    # Admin previously removed this — respect that, skip
                    continue
                # Already granted — nothing to do
                continue

            # Create new auto-assigned grant
            CustomerAllowedPaymentMethod.objects.create(
                customer=user,
                payment_method=method,
                auto_assigned=True,
                is_primary=False,  # Will set primary below
                granted_by=granted_by,
            )
            created_count += 1

        # Set the first default method as primary if customer has no primary yet
        has_primary = CustomerAllowedPaymentMethod.objects.filter(
            customer=user, is_primary=True,
        ).exists()
        if not has_primary:
            first = CustomerAllowedPaymentMethod.objects.filter(
                customer=user,
                admin_excluded=False,
            ).select_related('payment_method').order_by(
                '-payment_method__is_default',
                'payment_method__sort_order',
            ).first()
            if first:
                first.is_primary = True
                first.save(update_fields=['is_primary'])

        if created_count:
            log.info(
                "Auto-assigned %d default payment method(s) to customer %s",
                created_count, user.email,
            )


def sync_default_to_all_customers(payment_method, admin_user=None):
    """
    When a payment method is marked as default (is_default=True), assign it
    to all existing customers who don't already have it (and haven't excluded it).
    
    When is_default is set to False, we don't remove existing grants — those
    stay as they are. Admin must manually remove from individual customers.
    """
    from myapp.Models.Invoicing_models import CustomerAllowedPaymentMethod
    from myapp.Models.Auth_models import User

    if not payment_method.is_default or not payment_method.is_active:
        return  # Only sync when marking as default

    customers = User.objects.filter(role='customer', is_active=True)
    count = 0
    with dbtx.atomic():
        for customer in customers:
            existing = CustomerAllowedPaymentMethod.objects.filter(
                customer=customer,
                payment_method=payment_method,
            ).first()

            if existing:
                if existing.admin_excluded:
                    # Admin removed this for this customer — respect the exclusion
                    continue
                # Already has it
                continue

            CustomerAllowedPaymentMethod.objects.create(
                customer=customer,
                payment_method=payment_method,
                auto_assigned=True,
                is_primary=False,
                granted_by=admin_user,
            )
            count += 1

    log.info(
        "Synced default method '%s' to %d existing customers",
        payment_method.code, count,
    )
    return count
