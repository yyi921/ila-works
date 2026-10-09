from django.contrib import admin

from . import models

admin.site.register(models.Organisation, list_display=("name", "base_currency", "timezone"))

for model in (
    models.Account,
    models.Contact,
    models.TrackingCategory,
    models.TrackingOption,
    models.Document,
    models.DocumentLine,
    models.BankTransaction,
    models.BankTransactionLine,
    models.Journal,
    models.JournalLine,
    models.Dimension,
    models.DimensionValue,
    models.Event,
    models.Tag,
    models.ImportFile,
):
    admin.site.register(model, list_display=("__str__", "organisation", "source_system", "source_id"),
                        list_filter=("organisation", "source_system"))


class AssignmentShareInline(admin.TabularInline):
    model = models.AssignmentShare
    extra = 0


@admin.register(models.Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "organisation", "level", "method", "status", "set_by", "set_at")
    list_filter = ("organisation", "dimension", "level", "method", "status")
    inlines = [AssignmentShareInline]


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organisation", "user", "action", "succeeded", "input")
    list_filter = ("organisation", "action", "succeeded")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
