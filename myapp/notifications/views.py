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

from myapp.notifications.models import Notification
from myapp.notifications.serializers import NotificationSerializer


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
