"""WSGI config (fallback)."""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paybitnex.settings")
application = get_wsgi_application()
