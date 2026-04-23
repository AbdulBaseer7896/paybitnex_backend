"""
Central registry of premium features.

Adding a new paid feature is a three-line change:

  1. Add an entry to FEATURES below.
  2. Decorate the view / viewset with HasFeature("your_key").
  3. Wrap the frontend route / sidebar entry with the matching key.

Keys are short, lowercase, and stable — changing a key is equivalent
to a schema migration because it appears in CustomerFeatureAccess rows.
"""
from typing import Dict, Iterable, Set


# Registry of premium features. `default_enabled` is False for every
# real premium feature; flip it to True if you ever want a feature to
# be on by default for all verified customers (but still gateable off
# per-user). The `group` field lets the admin UI cluster related
# features together.
FEATURES: Dict[str, dict] = {
    "invoicing": {
        "label": "Invoicing",
        "description": (
            "Create and manage invoices, clients, and companies. "
            "Includes My Invoices, Create Invoice, My Clients, and "
            "My Companies tabs."
        ),
        "group": "Billing",
        "default_enabled": False,
    },
    # Future examples — add here as the product grows. No migration needed.
    # "bulk_payments": {
    #     "label": "Bulk Payments",
    #     "description": "Upload a CSV of payments and process them at once.",
    #     "group": "Payments",
    #     "default_enabled": False,
    # },
    # "api_access": {
    #     "label": "API Access",
    #     "description": "Generate API keys and integrate PayBitnex programmatically.",
    #     "group": "Developer",
    #     "default_enabled": False,
    # },
    # "advanced_reports": {
    #     "label": "Advanced Reports",
    #     "description": "Exportable analytics and custom date-range reports.",
    #     "group": "Reports",
    #     "default_enabled": False,
    # },
}


def all_feature_keys() -> Set[str]:
    return set(FEATURES.keys())


def is_valid_feature_key(key: str) -> bool:
    return key in FEATURES


def feature_registry_for_api() -> list:
    """Shape the registry for the /core/features/ endpoint."""
    return [
        {
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "group": meta.get("group", "Other"),
            "default_enabled": meta.get("default_enabled", False),
        }
        for key, meta in FEATURES.items()
    ]


def user_feature_map(user) -> Dict[str, bool]:
    """
    Return {feature_key: enabled_bool} for a user.

    Staff (admin + accountant) always get True for every feature — they
    don't have gates. For customers, we start from every feature's
    `default_enabled` value, then overlay whatever CustomerFeatureAccess
    rows exist (those are the admin's explicit per-user decisions).

    Sync version — safe to call from sync views. For async views
    (adrf / MeView), use auser_feature_map() instead; calling this from
    an async context after a customer has any grant rows will raise
    SynchronousOnlyOperation.
    """
    # Local import to avoid circular imports at module load time.
    from myapp.Models.Auth_models import UserRole

    # Start with defaults for every known feature.
    out = {
        key: bool(meta.get("default_enabled", False))
        for key, meta in FEATURES.items()
    }

    if not user or not getattr(user, "is_authenticated", False):
        return out

    # Staff: always-on for every feature. They see everything.
    if getattr(user, "role", None) in (UserRole.ADMIN, UserRole.ACCOUNTANT):
        return {key: True for key in FEATURES}

    # Customer: overlay explicit grants on top of defaults.
    from myapp.Models.Feature_models import CustomerFeatureAccess
    # list() forces the query to evaluate in this sync scope, which is
    # exactly what we want — callers that iterate the QuerySet later
    # (e.g. from an async serializer) would otherwise trip
    # SynchronousOnlyOperation.
    grants = list(CustomerFeatureAccess.objects.filter(user=user))
    for g in grants:
        if g.feature_key in out:
            out[g.feature_key] = bool(g.enabled)
    return out


async def auser_feature_map(user) -> Dict[str, bool]:
    """
    Async variant of user_feature_map() for use inside adrf / async
    views (e.g. MeView). Does the same thing but uses Django's async
    queryset iterator so the ORM call is legal in an async context.
    """
    from myapp.Models.Auth_models import UserRole

    out = {
        key: bool(meta.get("default_enabled", False))
        for key, meta in FEATURES.items()
    }

    if not user or not getattr(user, "is_authenticated", False):
        return out

    if getattr(user, "role", None) in (UserRole.ADMIN, UserRole.ACCOUNTANT):
        return {key: True for key in FEATURES}

    from myapp.Models.Feature_models import CustomerFeatureAccess
    async for g in CustomerFeatureAccess.objects.filter(user=user):
        if g.feature_key in out:
            out[g.feature_key] = bool(g.enabled)
    return out


def user_has_feature(user, feature_key: str) -> bool:
    """Quick single-feature check (sync). DRF permission classes run
    from sync view dispatch, so this is the right one for permissions."""
    return user_feature_map(user).get(feature_key, False)


def set_user_features(
    user,
    updates: Dict[str, bool],
    granted_by=None,
    notes: str = "",
) -> Dict[str, bool]:
    """
    Apply a batch of feature toggles for a user.

    `updates` is `{feature_key: enabled_bool}`. Invalid keys are ignored
    silently (prevents typos from leaking 500s to admins). Creates or
    updates CustomerFeatureAccess rows as needed.

    Returns the resulting feature map.
    """
    from myapp.Models.Feature_models import CustomerFeatureAccess

    valid = {k: bool(v) for k, v in (updates or {}).items() if k in FEATURES}

    for key, enabled in valid.items():
        CustomerFeatureAccess.objects.update_or_create(
            user=user, feature_key=key,
            defaults={
                "enabled": enabled,
                "granted_by": granted_by,
                "notes": notes or "",
            },
        )

    return user_feature_map(user)
