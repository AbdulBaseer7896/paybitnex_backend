"""
URL routing for Notification REST endpoints.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from myapp.notifications.views import NotificationViewSet

router = DefaultRouter()
router.register(r"", NotificationViewSet, basename="notification")

urlpatterns = [
    path("", include(router.urls)),
]
