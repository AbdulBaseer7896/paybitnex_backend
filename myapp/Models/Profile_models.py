"""
Customer profile — personal info + CNIC verification.
CNIC images stored in Cloudinary via default storage.
"""
import uuid
from django.db import models
from django.core.validators import RegexValidator


CNIC_VALIDATOR = RegexValidator(
    regex=r"^\d{5}-?\d{7}-?\d{1}$",
    message="CNIC must be in format 12345-1234567-1 or 13 digits.",
)


class CustomerProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        "myapp.User", on_delete=models.CASCADE, related_name="profile",
    )

    # Personal
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20)
    cnic_number = models.CharField(
        max_length=15, validators=[CNIC_VALIDATOR], unique=True,
    )
    cnic_front = models.ImageField(upload_to="cnic/front/")
    cnic_back = models.ImageField(upload_to="cnic/back/")
    selfie = models.ImageField(upload_to="cnic/selfie/", null=True, blank=True)

    # Address (optional, useful for KYC)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=80, blank=True)

    # KYC status — accountant/admin can approve
    KYC_PENDING = "pending"
    KYC_APPROVED = "approved"
    KYC_REJECTED = "rejected"
    KYC_OBJECTIONS = "objections"   # admin raised fixable issues
    KYC_RESUBMITTED = "resubmitted" # customer fixed and returned for re-review
    KYC_CHOICES = [
        (KYC_PENDING, "Pending Review"),
        (KYC_APPROVED, "Approved"),
        (KYC_REJECTED, "Rejected"),
        (KYC_OBJECTIONS, "Objections Raised"),
        (KYC_RESUBMITTED, "Resubmitted for Review"),
    ]
    kyc_status = models.CharField(
        max_length=20, choices=KYC_CHOICES, default=KYC_PENDING, db_index=True,
    )
    kyc_reviewed_by = models.ForeignKey(
        "myapp.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="kyc_reviews",
    )
    kyc_reviewed_at = models.DateTimeField(null=True, blank=True)
    kyc_notes = models.TextField(blank=True)
    # Objection workflow
    kyc_objections = models.JSONField(
        default=list, blank=True,
        help_text="List of active objection entries: [{field, message, raised_at, raised_by}]",
    )
    kyc_objection_round = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of objection rounds this profile has gone through.",
    )
    kyc_approved_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when KYC moves to APPROVED. Profile becomes locked after this.",
    )

    # ── Resubmission diff tracking ──
    # When a customer responds to objections by PATCHing
    # /accounts/profile/, we record which fields they actually
    # changed (as a list of field names: e.g. ['full_name',
    # 'selfie']) plus when they did it. The admin / accountant
    # review modal uses this to highlight exactly what's new in
    # the resubmission, so reviewers don't have to manually diff
    # the old vs new submission. Reset to [] / null when the
    # profile is approved or when fresh objections are raised
    # (the next round starts clean).
    kyc_last_resubmit_at = models.DateTimeField(null=True, blank=True)
    kyc_last_resubmit_changes = models.JSONField(
        default=list, blank=True,
        help_text="List of field names the customer changed in their "
                  "most recent resubmission (e.g. ['full_name', 'selfie']).",
    )

    # Customer scoring / rating — updated by signals on transaction completion.
    RATING_NEW = "new"
    RATING_BRONZE = "bronze"
    RATING_SILVER = "silver"
    RATING_GOLD = "gold"
    RATING_PLATINUM = "platinum"
    RATING_CHOICES = [
        (RATING_NEW, "New"),
        (RATING_BRONZE, "Bronze"),
        (RATING_SILVER, "Silver"),
        (RATING_GOLD, "Gold"),
        (RATING_PLATINUM, "Platinum"),
    ]
    rating_tier = models.CharField(
        max_length=20, choices=RATING_CHOICES, default=RATING_NEW, db_index=True,
    )
    rating_score = models.IntegerField(default=0)
    completed_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    total_volume_pkr = models.DecimalField(
        max_digits=16, decimal_places=2, default=0,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer_profiles"

    def __str__(self):
        return f"Profile: {self.full_name} ({self.user.email})"

    @property
    def is_locked(self):
        """True once KYC is approved — profile details cannot be edited."""
        return self.kyc_status == self.KYC_APPROVED
