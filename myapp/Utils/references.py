"""Human-friendly reference-number generation."""
from datetime import datetime
from django.db.models import Max


def next_reference(model_cls, prefix: str, field: str = "reference") -> str:
    """
    Generates 'PREFIX-YYYY-000001'. Thread-safe-enough for our volume;
    for higher scale swap in a dedicated sequence table.
    """
    year = datetime.now().year
    pattern = f"{prefix}-{year}-"
    last = (
        model_cls.objects
        .filter(**{f"{field}__startswith": pattern})
        .aggregate(m=Max(field))["m"]
    )
    if last:
        try:
            n = int(last.split("-")[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    else:
        n = 1
    return f"{pattern}{n:06d}"


async def anext_reference(model_cls, prefix: str, field: str = "reference") -> str:
    """Async version."""
    year = datetime.now().year
    pattern = f"{prefix}-{year}-"
    last_obj = await (
        model_cls.objects
        .filter(**{f"{field}__startswith": pattern})
        .order_by(f"-{field}")
        .afirst()
    )
    if last_obj:
        try:
            last = getattr(last_obj, field)
            n = int(last.split("-")[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    else:
        n = 1
    return f"{pattern}{n:06d}"
