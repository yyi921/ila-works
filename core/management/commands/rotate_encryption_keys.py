from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from core.fields import EncryptedTextField


class Command(BaseCommand):
    help = "Re-encrypt every encrypted field with the primary (first) key in FIELD_ENCRYPTION_KEYS."

    def handle(self, *args, **options):
        for model in apps.get_models():
            fields = [f.name for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)]
            if not fields:
                continue
            count = 0
            with transaction.atomic():
                for obj in model._default_manager.select_for_update().iterator():
                    obj.save(update_fields=fields)
                    count += 1
            self.stdout.write(f"{model._meta.label}: re-encrypted {count} row(s)")
