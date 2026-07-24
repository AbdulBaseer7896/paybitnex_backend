import django.urls.converters
import django.urls

_orig_register_converter = django.urls.converters.register_converter


def _safe_register_converter(converter, type_name):
    try:
        _orig_register_converter(converter, type_name)
    except ValueError as e:
        if "already registered" in str(e):
            pass
        else:
            raise


django.urls.converters.register_converter = _safe_register_converter
django.urls.register_converter = _safe_register_converter

from .celery import app as celery_app

__all__ = ("celery_app",)

