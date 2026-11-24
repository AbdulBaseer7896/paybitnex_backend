"""
Serializers for Notification REST endpoints.
"""
from rest_framework import serializers
from myapp.notifications.models import Notification, DeviceToken, DeviceType


class DeviceTokenSerializer(serializers.ModelSerializer):
    token = serializers.CharField(max_length=512)
    device_type = serializers.ChoiceField(
        choices=DeviceType.choices,
        default=DeviceType.ANDROID,
        required=False,
    )

    class Meta:
        model = DeviceToken
        fields = ["token", "device_type", "is_active", "created_at", "updated_at"]
        read_only_fields = ["is_active", "created_at", "updated_at"]

    def validate_token(self, value):
        if not value or not str(value).strip():
            raise serializers.ValidationError("Device token cannot be empty.")
        return str(value).strip()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "type",
            "payment",
            "title",
            "message",
            "payload",
            "is_read",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "type",
            "payment",
            "title",
            "message",
            "payload",
            "created_at",
        ]
