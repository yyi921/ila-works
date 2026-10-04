from datetime import timedelta

from django.db import models
from django.utils import timezone

from core.models import Organisation


class XeroConnection(models.Model):
    """Links a Xero tenant to a canonical Organisation and holds its OAuth tokens."""

    organisation = models.OneToOneField(Organisation, on_delete=models.CASCADE, related_name="xero_connection")
    tenant_id = models.UUIDField(unique=True)
    tenant_name = models.CharField(max_length=255, blank=True)
    tenant_type = models.CharField(max_length=50, blank=True)
    xero_connection_id = models.UUIDField(null=True, blank=True)
    access_token = models.TextField()
    refresh_token = models.TextField()
    id_token = models.TextField(blank=True)
    scopes = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.tenant_name or str(self.tenant_id)

    def is_access_token_expired(self, leeway_seconds=60):
        return timezone.now() >= self.expires_at - timedelta(seconds=leeway_seconds)
