"""Customer profile — CNIC + selfie + personal details + KYC."""
from rest_framework import serializers
from myapp.Models.Profile_models import CustomerProfile, CNIC_VALIDATOR


class CustomerProfileSerializer(serializers.ModelSerializer):
    cnic_front_url = serializers.SerializerMethodField()
    cnic_back_url = serializers.SerializerMethodField()
    selfie_url = serializers.SerializerMethodField()
    user_email = serializers.CharField(source="user.email", read_only=True)
    kyc_reviewed_by_email = serializers.CharField(
        source="kyc_reviewed_by.email", read_only=True, default=None,
    )
    is_locked = serializers.BooleanField(read_only=True)

    # Declare cnic_number WITHOUT the auto UniqueValidator (it's sync
    # and would crash async views). We check uniqueness manually below.
    cnic_number = serializers.CharField(
        max_length=15, validators=[CNIC_VALIDATOR],
    )

    class Meta:
        model = CustomerProfile
        fields = [
            "id", "user", "user_email",
            "full_name", "phone", "cnic_number",
            "cnic_front", "cnic_back", "selfie",
            "cnic_front_url", "cnic_back_url", "selfie_url",
            "address", "city",
            "kyc_status", "kyc_notes", "kyc_reviewed_at",
            "kyc_reviewed_by_email",
            "kyc_objections", "kyc_objection_round", "kyc_approved_at",
            "is_locked",
            "rating_tier", "rating_score",
            "completed_count", "rejected_count", "total_volume_pkr",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "user", "user_email",
            "kyc_status", "kyc_notes", "kyc_reviewed_at",
            "kyc_reviewed_by_email",
            "kyc_objections", "kyc_objection_round", "kyc_approved_at",
            "is_locked",
            "cnic_front_url", "cnic_back_url", "selfie_url",
            "rating_tier", "rating_score",
            "completed_count", "rejected_count", "total_volume_pkr",
            "created_at", "updated_at",
        ]
        extra_kwargs = {
            "cnic_front": {"write_only": True, "required": True},
            "cnic_back":  {"write_only": True, "required": True},
            "selfie":     {"write_only": True, "required": True,
                           "allow_null": False},
        }

    def validate_cnic_number(self, value):
        qs = CustomerProfile.objects.filter(cnic_number=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "This CNIC is already registered to another account."
            )
        return value

    def validate(self, attrs):
        # Once KYC is approved, profile is locked — no edits allowed.
        if self.instance and self.instance.is_locked:
            raise serializers.ValidationError(
                "Profile is locked after KYC approval and cannot be edited."
            )
        return attrs

    def get_cnic_front_url(self, obj):
        return obj.cnic_front.url if obj.cnic_front else None

    def get_cnic_back_url(self, obj):
        return obj.cnic_back.url if obj.cnic_back else None

    def get_selfie_url(self, obj):
        return obj.selfie.url if obj.selfie else None


class KYCReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        CustomerProfile.KYC_APPROVED,
        CustomerProfile.KYC_REJECTED,
        CustomerProfile.KYC_PENDING,
    ])
    notes = serializers.CharField(required=False, allow_blank=True)


class KYCObjectionItemSerializer(serializers.Serializer):
    """Single objection entry: which field, what's wrong."""
    field = serializers.CharField(
        max_length=60,
        help_text="e.g. 'selfie', 'cnic_front', 'cnic_back', 'address', 'full_name', 'general'",
    )
    message = serializers.CharField(max_length=500)


class KYCRaiseObjectionsSerializer(serializers.Serializer):
    """Admin raises one or more objections against a KYC profile."""
    objections = KYCObjectionItemSerializer(many=True, required=True, min_length=1)
    notes = serializers.CharField(required=False, allow_blank=True)


class CustomerScoreSerializer(serializers.Serializer):
    """Computed score for a customer. Read-only."""
    score = serializers.IntegerField()
    grade = serializers.CharField()
    tier = serializers.CharField()
    total_transactions = serializers.IntegerField()
    completed = serializers.IntegerField()
    rejected = serializers.IntegerField()
    completion_rate = serializers.FloatField()
    rejection_rate = serializers.FloatField()
    total_volume_pkr = serializers.CharField()
    breakdown = serializers.DictField()
    notes = serializers.CharField(required=False)
