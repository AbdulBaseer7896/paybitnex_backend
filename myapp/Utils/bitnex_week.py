"""
Bitnex week — a company-wide, configurable weekly period used for closing
reports and customer/accountant default date filters.

Stored in SystemSetting under two keys:
    bitnex_week_start_day  -> int 0..6  (0 = Monday … 6 = Sunday), default 0
    bitnex_week_name       -> str label, default "Bitnex week"

The week is always 7 days long; only the start day is configurable. e.g. a
start day of Tuesday (1) means each Bitnex week runs Tue → Mon.
"""
from datetime import date, timedelta

from myapp.Models.Core_models import SystemSetting

WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

DEFAULT_START_DAY = 0          # Monday
DEFAULT_WEEK_NAME = "Bitnex week"


def get_week_config():
    """Return the current Bitnex-week config as a dict."""
    raw_start = SystemSetting.get("bitnex_week_start_day", str(DEFAULT_START_DAY))
    try:
        start_day = int(raw_start)
    except (TypeError, ValueError):
        start_day = DEFAULT_START_DAY
    if start_day < 0 or start_day > 6:
        start_day = DEFAULT_START_DAY
    name = SystemSetting.get("bitnex_week_name", DEFAULT_WEEK_NAME) or DEFAULT_WEEK_NAME
    return {
        "start_day": start_day,
        "start_day_name": WEEKDAY_NAMES[start_day],
        "name": name,
    }


def current_week_range(on=None):
    """Return (from_date, to_date) for the Bitnex week containing `on`.

    `from_date` is the most recent configured start-day on or before `on`;
    `to_date` is `from_date` + 6 days (a full 7-day week). Both are
    `datetime.date`.
    """
    today = on or date.today()
    cfg = get_week_config()
    start_day = cfg["start_day"]
    # Python weekday(): Monday=0 … Sunday=6 — matches our convention.
    delta = (today.weekday() - start_day) % 7
    week_start = today - timedelta(days=delta)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end
