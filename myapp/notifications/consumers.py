"""
WebSocket consumer for real-time notifications.

Groups:
  - ``user_<id>``  — every authenticated user joins their personal group.
  - ``admins``     — users with role ADMIN or ACCOUNTANT also join this group.

A customer NEVER joins another customer's group or the admins group.

Incoming messages:
  - ``{"type": "ping"}``  → replies ``{"type": "pong"}``
  - Everything else is silently ignored (the socket is server-push only).

Outgoing group messages:
  - ``{"type": "notification.send", "payload": {...}}``
    The consumer forwards ``payload`` as-is to the client.
"""
import json
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from myapp.Models.Auth_models import UserRole

logger = logging.getLogger(__name__)


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """Server-push notification channel with ping/pong keep-alive."""

    # Set during connect(); used in disconnect() to leave the right groups.
    _groups: list[str]

    async def connect(self):
        user = self.scope.get("user")

        # The auth middleware already rejects unauthenticated connections
        # before they reach here, but guard defensively.
        if user is None or user.is_anonymous:
            await self.close(code=4401)
            return

        self._groups = [f"user_{user.pk}"]

        if user.role in (UserRole.ADMIN, UserRole.ACCOUNTANT):
            self._groups.append("admins")

        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()
        logger.debug(
            "ws connect: user=%s groups=%s",
            user.pk,
            self._groups,
        )

    async def disconnect(self, code):
        for group in getattr(self, "_groups", []):
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception:
                # Channel layer might already be torn down; don't crash.
                pass

    # ---- incoming from client ----

    async def receive_json(self, content, **kwargs):
        """Handle client-sent JSON frames.  Only ping is supported."""
        msg_type = content.get("type", "")
        if msg_type == "ping":
            await self.send_json({"type": "pong"})
            return
        # Silently drop anything else — the socket is server-push only.

    # ---- outgoing from channel layer (group_send) ----

    async def notification_send(self, event):
        """Forward a notification payload to the connected client.

        Triggered by a channel-layer group_send with
        ``{"type": "notification.send", "payload": {...}}``.
        """
        await self.send_json(event["payload"])
