from django.core.management.base import BaseCommand

from console.reference import sync_command_reference


class Command(BaseCommand):
    help = "Refresh the native command reference in the admin from the command registry."

    def handle(self, *args, **options):
        created, updated, retired = sync_command_reference()
        self.stdout.write(f"Command reference: {created} added, {updated} refreshed, {retired} marked removed.")
