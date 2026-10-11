from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_reference(sender, **kwargs):
    from django.db import DatabaseError

    from .reference import sync_command_reference

    try:
        sync_command_reference()
    except DatabaseError:  # table not there (e.g. migrating backwards); the next migrate fills it
        pass


class ConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "console"

    def ready(self):
        from . import commands  # noqa: F401  (registers the commands)

        post_migrate.connect(_sync_reference, sender=self)
