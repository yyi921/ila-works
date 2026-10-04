from django.urls import path

from . import views

app_name = "xero"

urlpatterns = [
    path("", views.status, name="status"),
    path("connect/", views.connect, name="connect"),
    path("callback/", views.callback, name="callback"),
    path("disconnect/<int:pk>/", views.disconnect, name="disconnect"),
]
