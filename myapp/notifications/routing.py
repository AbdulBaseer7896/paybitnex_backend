"""WebSocket URL routing for the notifications consumer."""
from django.urls import re_path

from myapp.notifications.consumers import NotificationConsumer

websocket_urlpatterns = [
    re_path(r"^ws/notifications/$", NotificationConsumer.as_asgi()),
]
