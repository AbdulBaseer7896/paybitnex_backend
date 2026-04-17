"""
Auto-activity tracking.

Hooks post_save / post_delete signals for every `myapp` model. Every
create/update/delete produces an AuditLog row with:
  - who did it (from the request via AuditMiddleware)
  - what model + PK
  - human-readable label
  - before/after snapshots showing only fields that actually changed
  - IP + user-agent

Fails silently — never blocks the underlying save.
"""
import logging
from threading import local

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

from myapp.Models.Audit_models import AuditLog

log = logging.getLogger(__name__)

# Thread-local holding the current HTTP request (set by AuditMiddleware).
_request_ctx = local()

# Detect which AuditLog columns actually exist in the INSTALLED DB schema.
# The model definition may declare fields that haven't been migrated yet,
# so we introspect the table directly. Falls back to model field names on
# any introspection failure (e.g., during initial migrate, before table
# exists).
def _detect_audit_columns():
    try:
        from django.db import connection
        with connection.cursor() as cur:
            desc = connection.introspection.get_table_description(
                cur, AuditLog._meta.db_table,
            )
            return {col.name for col in desc}
    except Exception:
        return {f.name for f in AuditLog._meta.get_fields()}


_AUDIT_COLS = _detect_audit_columns()
_HAS_TARGET_LABEL = "target_label" in _AUDIT_COLS
_HAS_METADATA     = "metadata" in _AUDIT_COLS


def _audit_create(**kwargs):
    """Create an AuditLog row, dropping fields not present in the DB."""
    if not _HAS_TARGET_LABEL:
        kwargs.pop("target_label", None)
    if not _HAS_METADATA:
        kwargs.pop("metadata", None)
    return AuditLog.objects.create(**kwargs)


def set_current_request(request):
    _request_ctx.request = request


def get_current_request():
    return getattr(_request_ctx, "request", None)


def _current_actor():
    req = get_current_request()
    if req is None:
        return None, None, ""
    user = getattr(req, "user", None)
    if user is not None and not user.is_authenticated:
        user = None
    ip = (req.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
          or req.META.get("REMOTE_ADDR"))
    ua = req.META.get("HTTP_USER_AGENT", "")[:300]
    return user, ip, ua


# Models we do NOT auto-track (would either recurse or be pure read-model).
SKIP_MODELS = {
    "AuditLog",
    "DailyReport", "WeeklyReport", "MonthlyReport",
    "ExchangeRateHistory",
    "TransactionStatusHistory",
}

# Never leak these into before/after snapshots.
SENSITIVE_FIELDS = {"password", "last_login"}


def _is_tracked(model):
    if model.__name__ in SKIP_MODELS:
        return False
    return model._meta.app_label == "myapp"


def _snapshot(instance):
    out = {}
    for field in instance._meta.fields:
        if field.name in SENSITIVE_FIELDS:
            continue
        try:
            value = getattr(instance, field.attname, None)
        except Exception:
            value = None
        if value is None or isinstance(value, (str, int, float, bool)):
            out[field.name] = value
        else:
            out[field.name] = str(value)
    return out


def _diff(before, after):
    """Return {field: {'from': x, 'to': y}} for only changed fields."""
    if not before:
        return {k: {"from": None, "to": v} for k, v in after.items()}
    changed = {}
    for k, v in after.items():
        old = before.get(k)
        if old != v:
            changed[k] = {"from": old, "to": v}
    return changed


@receiver(pre_save)
def _cache_original(sender, instance, **kwargs):
    if not _is_tracked(sender):
        return
    if instance.pk is None:
        instance._audit_original = None
        return
    try:
        original = sender.objects.filter(pk=instance.pk).first()
        instance._audit_original = _snapshot(original) if original else None
    except Exception as e:
        log.warning("audit pre_save snapshot failed: %s", e)
        instance._audit_original = None


@receiver(post_save)
def _on_save(sender, instance, created, **kwargs):
    if not _is_tracked(sender):
        return
    try:
        user, ip, ua = _current_actor()
        after = _snapshot(instance)
        label = f"{sender.__name__} {str(instance)[:160]}"

        if created:
            _audit_create(
                user=user,
                action=AuditLog.ACTION_CREATE,
                target_model=sender.__name__,
                target_id=str(instance.pk),
                target_label=label,
                description=f"{sender.__name__} created",
                after=after,
                metadata={"auto": True},
                ip_address=ip, user_agent=ua,
            )
        else:
            before = getattr(instance, "_audit_original", None)
            diff = _diff(before or {}, after)
            if not diff:
                return
            _audit_create(
                user=user,
                action=AuditLog.ACTION_UPDATE,
                target_model=sender.__name__,
                target_id=str(instance.pk),
                target_label=label,
                description=f"{sender.__name__} updated",
                before={k: v["from"] for k, v in diff.items()},
                after={k: v["to"] for k, v in diff.items()},
                metadata={"auto": True, "changed_fields": list(diff.keys())},
                ip_address=ip, user_agent=ua,
            )
    except Exception as e:
        log.warning("audit post_save failed for %s: %s", sender.__name__, e)


@receiver(post_delete)
def _on_delete(sender, instance, **kwargs):
    if not _is_tracked(sender):
        return
    try:
        user, ip, ua = _current_actor()
        _audit_create(
            user=user,
            action=AuditLog.ACTION_DELETE,
            target_model=sender.__name__,
            target_id=str(instance.pk),
            target_label=f"{sender.__name__} {str(instance)[:160]}",
            description=f"{sender.__name__} deleted",
            before=_snapshot(instance),
            metadata={"auto": True},
            ip_address=ip, user_agent=ua,
        )
    except Exception as e:
        log.warning("audit post_delete failed for %s: %s", sender.__name__, e)
