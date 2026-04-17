"""Celery configuration for PayBitnex."""
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paybitnex.settings")

app = Celery("paybitnex")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
