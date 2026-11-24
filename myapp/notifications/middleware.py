"""
WebSocket JWT authentication middleware for Django Channels.

Accepts the JWT token via:
  1. The ``Authorization: Bearer <token>`` header (standard), or
  2. The ``?token=<token>`` query-string parameter (mobile clients that
     cannot set custom headers on the WebSocket handshake).

Security:
  - The raw token and the full URL (which may contain the token) are
    NEVER logged.  Every log line uses a masked placeholder instead.
  - Unauthenticated or invalid-token connections are rejected with
    WebSocket close code **4401**.
"""
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)

# Custom close code mirroring HTTP 401 — the mobile client can switch on
# this to distinguish "need to re-authenticate" from other failures.
WS_CLOSE_UNAUTHENTICATED = 4401


@database_sync_to_async
def _get_user_from_token(raw_token: str):
    """Validate a raw JWT string and return the User, or None."""
    # Import here so the module loads even if simplejwt isn't configured
    # (e.g. during collectstatic or migration).
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import TokenError
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        validated = AccessToken(raw_token)
        user_id = validated["user_id"]
        return User.objects.get(pk=user_id)
    except (TokenError, KeyError, User.DoesNotExist):
        return None


def _extract_token(scope) -> str | None:
    """Pull the JWT from headers or query string.  Never log the value."""
    # 1. Try the Authorization header.
    headers = dict(scope.get("headers", []))
    auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    # 2. Fall back to ?token= query parameter.
    qs = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
    tokens = qs.get("token", [])
    if tokens:
        return tokens[0]

    return None


class JWTAuthMiddleware(BaseMiddleware):
    """Channels middleware that authenticates the WebSocket handshake.

    On success ``scope["user"]`` is set to the authenticated User.
    On failure the connection is accepted (Channels requires this before
    sending a close frame) and immediately closed with code 4401.
    """

    async def __call__(self, scope, receive, send):
        raw_token = _extract_token(scope)

        if raw_token is None:
            logger.debug("ws auth: no token provided — rejecting")
            scope["user"] = AnonymousUser()
            await send({"type": "websocket.close", "code": WS_CLOSE_UNAUTHENTICATED})
            return

        user = await _get_user_from_token(raw_token)

        if user is None:
            # Intentionally vague — do NOT say "expired" vs "invalid".
            logger.debug("ws auth: token validation failed — rejecting")
            scope["user"] = AnonymousUser()
            await send({"type": "websocket.close", "code": WS_CLOSE_UNAUTHENTICATED})
            return

        # Reject inactive users even if the token is technically valid.
        if not user.is_active:
            logger.debug("ws auth: user is inactive — rejecting")
            scope["user"] = AnonymousUser()
            await send({"type": "websocket.close", "code": WS_CLOSE_UNAUTHENTICATED})
            return

        scope["user"] = user
        return await super().__call__(scope, receive, send)
