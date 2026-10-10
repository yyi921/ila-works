from django.contrib import admin
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView


def healthz(request):
    """Liveness check for the hosting platform: the app is up and can reach the database."""
    connection.ensure_connection()
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz/", healthz),
    path("admin/", admin.site.urls),
    path("xero/", include("xero.urls")),
    path("console/", include("console.urls")),
    path("", RedirectView.as_view(pattern_name="console:console", permanent=False)),
]
