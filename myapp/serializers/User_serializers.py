"""User + auth serializers."""
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password

from myapp.Models.Auth_models import User, UserRole


class UserBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "role", "is_profile_complete"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """Read serializer — includes a profile-picture URL if present."""
    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "phone",
            "is_active", "is_profile_complete",
            "profile_picture_url",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "created_at", "updated_at", "is_profile_complete",
            "profile_picture_url",
        ]

    def get_profile_picture_url(self, obj):
        # Explicit profile picture takes priority.
        if getattr(obj, "profile_picture", None):
            try:
                return obj.profile_picture.url
            except Exception:
                pass
        # Fallback: for customers, use their selfie from the KYC profile.
        try:
            profile = obj.profile
            if profile and profile.selfie:
                return profile.selfie.url
        except Exception:
            return None
        return None


class AdminCreateUserSerializer(serializers.ModelSerializer):
    """Admin creates a new user (any role). Temporary password returned."""
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "email", "full_name", "role", "phone",
            "password", "is_active",
        ]
        read_only_fields = ["id"]

    def validate_role(self, value):
        if value not in UserRole.values:
            raise serializers.ValidationError("Invalid role.")
        return value

    # Manual email-uniqueness check so this stays safe inside async views.
    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with that email already exists."
            )
        return value

    def create(self, validated_data):
        password = (
            validated_data.pop("password", None)
            or User.objects.make_random_password()
        )
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        # expose the temp password to the creator (only in the response)
        user._plain_password = password
        return user


class AdminUpdateUserSerializer(serializers.ModelSerializer):
    """Admin edits an existing user."""
    class Meta:
        model = User
        fields = ["full_name", "phone", "role", "is_active"]

    def validate_role(self, value):
        if value not in UserRole.values:
            raise serializers.ValidationError("Invalid role.")
        return value


class AdminResetPasswordSerializer(serializers.Serializer):
    """Admin forces a new password for a user.

    Either supply `new_password` or leave blank for one to be generated.
    """
    new_password = serializers.CharField(required=False, allow_blank=True)

    def validate_new_password(self, value):
        if value:
            validate_password(value)
        return value


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True)

    def validate_new_password(self, value):
        validate_password(value)
        return value


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Include role + profile-complete flag in the token payload."""
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["email"] = user.email
        token["is_profile_complete"] = user.is_profile_complete
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserBriefSerializer(self.user).data
        return data
