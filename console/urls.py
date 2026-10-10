from django.urls import path

from . import views

app_name = "console"

urlpatterns = [
    path("", views.console, name="console"),
    path("run/", views.run, name="run"),
]
