"""Dispatch URLs — companies + drivers + loads."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from myapp.Views.Dispatch_views import (
    DispatchCompanyViewSet, DispatchDriverViewSet, DispatchViewSet,
)

router = DefaultRouter()
router.register(r"companies", DispatchCompanyViewSet,
                basename="dispatch-companies")
router.register(r"drivers", DispatchDriverViewSet,
                basename="dispatch-drivers")
router.register(r"loads", DispatchViewSet,
                basename="dispatch-loads")

urlpatterns = [
    path("", include(router.urls)),
]
