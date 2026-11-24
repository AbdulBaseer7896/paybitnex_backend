"""
REST ViewSet for Notifications.

Endpoints:
  GET   /api/v1/notifications/             (paginated, optional ?unread=true)
  GET   /api/v1/notifications/unread-count/
  PATCH /api/v1/notifications/{id}/read/
  POST  /api/v1/notifications/read-all/

Security:
  - Users can strictly access ONLY their own notifications.
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from myapp.notifications.models import Notification, DeviceToken
from myapp.notifications.serializers import NotificationSerializer, DeviceTokenSerializer


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        # Strict scope: Users can only see their own notifications
        qs = Notification.objects.filter(recipient=self.request.user)

        # Optional filter for unread notifications
        unread = self.request.query_params.get("unread", "").lower()
        if unread in ("true", "1", "yes"):
            qs = qs.filter(is_read=False)

        return qs.order_by("-created_at")

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        """
        GET /api/v1/notifications/unread-count/
        Returns count of unread notifications for the authenticated user.
        """
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return Response({"unread_count": count})

    @action(detail=True, methods=["patch"], url_path="read")
    def mark_read(self, request, pk=None):
        """
        PATCH /api/v1/notifications/{id}/read/
        Marks a specific notification as read.
        """
        notification = self.get_object()  # Honors get_queryset scoping (404 if not owned)
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])

        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        """
        POST /api/v1/notifications/read-all/
        Marks all unread notifications for the authenticated user as read.
        """
        updated_count = Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        ).update(is_read=True)

        return Response({
            "message": "All notifications marked as read.",
            "updated_count": updated_count,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="devices")
    def register_device(self, request):
        """
        POST /api/v1/notifications/devices/
        Registers or updates an FCM device token for background push notifications.

        Body:
            {
                "token": "fcm_registration_token_here",
                "device_type": "android" | "ios" | "web"
            }
        """
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["token"]
        device_type = serializer.validated_data.get("device_type", "android")

        device, created = DeviceToken.objects.update_or_create(
            token=token,
            defaults={
                "user": request.user,
                "device_type": device_type,
                "is_active": True,
            },
        )

        return Response(
            {
                "message": "Device token registered successfully.",
                "created": created,
                "device_type": device.device_type,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["delete", "post"], url_path="devices/unregister")
    def unregister_device(self, request):
        """
        POST or DELETE /api/v1/notifications/devices/unregister/
        Deactivates an FCM device token (call on user logout).

        Body:
            {
                "token": "fcm_registration_token_here"
            }
        """
        token = request.data.get("token", "").strip()
        if not token:
            return Response(
                {"detail": "token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = DeviceToken.objects.filter(
            user=request.user,
            token=token,
        ).update(is_active=False)

        return Response(
            {"message": "Device token unregistered successfully.", "updated": updated},
            status=status.HTTP_200_OK,
        )
