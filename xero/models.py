from datetime import timedelta

from django.db import models
from django.utils import timezone

from core.fields import EncryptedTextField
from core.models import Organisation


class XeroConnection(models.Model):
    """Links a Xero tenant to a canonical Organisation and holds its OAuth tokens (encrypted at rest)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Connected"
        REVOKED = "revoked", "Disconnected in Xero"

    organisation = models.OneToOneField(Organisation, on_delete=models.CASCADE, related_name="xero_connection")
    tenant_id = models.UUIDField(unique=True)
    tenant_name = models.CharField(max_length=255, blank=True)
    tenant_type = models.CharField(max_length=50, blank=True)
    xero_connection_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    status_changed_at = models.DateTimeField(default=timezone.now)
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    id_token = EncryptedTextField(blank=True)
    scopes = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.tenant_name or str(self.tenant_id)

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    def is_access_token_expired(self, leeway_seconds=60):
        return timezone.now() >= self.expires_at - timedelta(seconds=leeway_seconds)
