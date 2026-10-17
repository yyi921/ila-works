from django.urls import path

from . import builder_views, views

app_name = "console"

urlpatterns = [
    path("", views.console, name="console"),
    path("run/", views.run, name="run"),
    path("files/<int:pk>/download/", views.file_download, name="file_download"),
    path("createtag/<int:pk>/template/", views.createtag_template, name="createtag_template"),
    path("builder/", builder_views.home, name="builder"),
    path("builder/new/<slug:template_key>/", builder_views.new, name="builder_new"),
    path("builder/<int:pk>/", builder_views.edit, name="builder_edit"),
    path("builder/<int:pk>/delete/", builder_views.delete, name="builder_delete"),
]
