"""
Serializers for Notification REST endpoints.
"""
from rest_framework import serializers
from myapp.notifications.models import Notification


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
