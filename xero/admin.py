from django.contrib import admin

from .models import XeroConnection


@admin.register(XeroConnection)
class XeroConnectionAdmin(admin.ModelAdmin):
    list_display = ("tenant_name", "organisation", "tenant_id", "expires_at", "updated_at")
    exclude = ("access_token", "refresh_token", "id_token")
    readonly_fields = ("organisation", "tenant_id", "xero_connection_id", "scopes", "expires_at")
