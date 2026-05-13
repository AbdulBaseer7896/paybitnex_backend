"""
Signals for automatic payment method assignment.

post_save on User:
    When a new customer is created (created=True, role='customer'),
    auto-assign all is_default=True payment methods.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

log = logging.getLogger(__name__)


def register():
    """Call once from AppConfig.ready() to activate these signals."""

    @receiver(post_save, dispatch_uid="auto_assign_payment_methods_on_user_create")
    def _auto_assign_on_customer_create(sender, instance, created, **kwargs):
        """Auto-assign default payment methods when a new customer is created."""
        from myapp.Models.Auth_models import User
        if sender is not User:
            return
        if not created:
            return
        if getattr(instance, 'role', None) != 'customer':
            return
        try:
            from myapp.Utils.auto_assign_payment_methods import assign_defaults_to_user
            assign_defaults_to_user(instance, granted_by=None)
        except Exception as e:
            # Never crash registration because of payment method assignment
            log.error(
                "Failed to auto-assign payment methods for new customer %s: %s",
                instance.email, e,
            )
