"""PayBitnex root URL configuration."""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import (
    SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView,
)

api_v1 = [
    path("auth/", include("myapp.Urls.Auth_urls")),
    path("accounts/", include("myapp.Urls.Account_urls")),
    path("banking/", include("myapp.Urls.Banking_urls")),
    path("transactions/", include("myapp.Urls.Transaction_urls")),
    path("rates/", include("myapp.Urls.Rate_urls")),
    path("fees/", include("myapp.Urls.Fee_urls")),
    path("partners/", include("myapp.Urls.Partner_urls")),
    path("expenses/", include("myapp.Urls.Expense_urls")),
    path("reports/", include("myapp.Urls.Report_urls")),
    path("core/", include("myapp.Urls.Core_urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema")),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
