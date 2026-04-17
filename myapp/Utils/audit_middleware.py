"""
Audit middleware.

Stashes the current request in a thread-local so the signal-based
activity logger (`myapp/Utils/activity_signals.py`) knows who's
performing each database write.

Supports both sync and async views transparently.
"""
from asgiref.sync import iscoroutinefunction
from myapp.Utils.activity_signals import set_current_request


SKIP_PATHS = (
    "/api/schema",
    "/api/docs",
    "/api/redoc",
    "/static/",
    "/media/",
)


class AuditMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)

    def __call__(self, request):
        if self._is_async:
            return self.__acall__(request)
        self._stash(request)
        try:
            return self.get_response(request)
        finally:
            set_current_request(None)

    async def __acall__(self, request):
        self._stash(request)
        try:
            return await self.get_response(request)
        finally:
            set_current_request(None)

    def _stash(self, request):
        if any(request.path.startswith(p) for p in SKIP_PATHS):
            set_current_request(None)
            return
        set_current_request(request)
