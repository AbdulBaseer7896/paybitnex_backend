"""
Customer scoring — auto-calculated reputation/quality score for customers.

Inputs (weighted):
  - Completed transaction count           (more = better)
  - Completion rate (completed / total)   (higher = better)
  - Rejection rate (rejected / total)     (lower = better, penalty)
  - Total volume in PKR                   (more = better)
  - Account age in days                   (older = more trust)

Output: integer 0–100 and a letter grade.

This is a pure-function module; views call `compute_score(user)` whenever
they need an up-to-date value. No persisted score table — it's always
computed on demand to stay consistent with the ledger.
"""
from decimal import Decimal
from django.utils import timezone

from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus


def _score_to_grade(score):
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


def compute_score(user):
    """Return a dict with score, grade, and the breakdown."""
    qs = IncomingPayment.objects.filter(customer=user)
    total = qs.count()
    if total == 0:
        return {
            "score": 50,
            "grade": "—",
            "tier": "new",
            "total_transactions": 0,
            "completed": 0,
            "rejected": 0,
            "completion_rate": 0,
            "rejection_rate": 0,
            "total_volume_pkr": "0",
            "breakdown": {
                "completions":    0,
                "completion_rate":0,
                "rejection_penalty":0,
                "volume":         0,
                "tenure":         0,
            },
            "notes": "New customer — no transactions yet.",
        }

    completed = qs.filter(status=TransactionStatus.COMPLETED).count()
    rejected  = qs.filter(status=TransactionStatus.REJECTED).count()
    completion_rate = (completed / total) if total else 0
    rejection_rate  = (rejected / total) if total else 0

    total_pkr = Decimal("0")
    for p in qs.filter(status=TransactionStatus.COMPLETED):
        if p.net_pkr:
            total_pkr += p.net_pkr

    # Component scores (each weighted)
    #
    # 1. Completions weight        25 pts — log curve, plateaus around 50 tx
    import math
    completion_pts = min(25, int(math.log10(max(1, completed)) * 14))

    # 2. Completion rate           30 pts — linear
    rate_pts = int(30 * completion_rate)

    # 3. Rejection penalty         -25 pts — linear
    penalty_pts = -int(25 * rejection_rate)

    # 4. Volume                    20 pts — log curve, plateaus at 10M PKR
    vol_pts = 0
    if total_pkr > 0:
        vol_pts = min(20, int(math.log10(float(total_pkr) + 1) * 3))

    # 5. Tenure in days            15 pts — plateaus at 1 year
    days = (timezone.now() - user.date_joined).days if hasattr(user, "date_joined") \
        else (timezone.now() - user.created_at).days
    tenure_pts = min(15, int(days / 365 * 15))

    raw = 25 + completion_pts + rate_pts + penalty_pts + vol_pts + tenure_pts
    score = max(0, min(100, raw))

    tier = ("vip" if score >= 90 else
            "trusted" if score >= 70 else
            "standard" if score >= 50 else
            "caution" if score >= 30 else
            "high-risk")

    return {
        "score": score,
        "grade": _score_to_grade(score),
        "tier": tier,
        "total_transactions": total,
        "completed": completed,
        "rejected": rejected,
        "completion_rate": round(completion_rate * 100, 1),
        "rejection_rate":  round(rejection_rate * 100, 1),
        "total_volume_pkr": str(total_pkr),
        "breakdown": {
            "completions":    completion_pts,
            "completion_rate": rate_pts,
            "rejection_penalty": penalty_pts,
            "volume":          vol_pts,
            "tenure":          tenure_pts,
        },
    }
