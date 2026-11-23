"""
ASGI config — async entry point.

Routes:
  - HTTP  → standard Django ASGI application
  - WebSocket → Django Channels with JWT auth middleware
"""
import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paybitnex.settings")

# Initialize Django BEFORE importing anything that touches models/settings.
django_asgi_app = get_asgi_application()

# These imports MUST come after get_asgi_application() so that Django is
# fully bootstrapped (apps loaded, settings available).
from myapp.notifications.middleware import JWTAuthMiddleware  # noqa: E402
from myapp.notifications.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": JWTAuthMiddleware(
            URLRouter(websocket_urlpatterns)
        ),
    }
)
