from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from . import models

class InvoiceSetupInline(admin.TabularInline):
    model = models.InvoiceSetup
    fields = ("name", "system", "import_format", "tax_basis", "is_active")
    extra = 0
    show_change_link = True
    verbose_name = "Invoice set-up"
    verbose_name_plural = "Invoice set-ups (one per system and kind of invoice; open one to edit its items)"


@admin.register(models.Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "timezone")
    inlines = [InvoiceSetupInline]


class InvoiceItemInline(admin.TabularInline):
    model = models.InvoiceItem
    fields = ("sort_order", "name", "aliases", "unit", "rate", "markup_percent", "account_code", "tax_type",
              "item_code", "notes")
    extra = 1


@admin.register(models.InvoiceSetup)
class InvoiceSetupAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation", "system", "import_format", "tax_basis", "is_active")
    list_filter = ("organisation", "system", "is_active")
    exclude = ("source_system", "source_id", "source_updated_at", "source_hash")
    inlines = [InvoiceItemInline]
    fieldsets = (
        (None, {"fields": ("organisation", "name", "system", "import_format", "is_active")}),
        ("Defaults", {"fields": ("tax_basis", "date_format", "due_days", "branding_theme", "default_account_code",
                                 "default_tax_type", "tracking_name", "reference_pattern")}),
        ("How invoices are made", {"fields": ("sources", "questions", "instructions")}),
    )


@admin.register(models.ImportFormat)
class ImportFormatAdmin(admin.ModelAdmin):
    list_display = ("name", "system", "document_type", "file_type", "is_active")
    list_filter = ("system", "document_type")


@admin.register(models.ImportFile)
class ImportFileAdmin(admin.ModelAdmin):
    list_display = ("file_name", "organisation", "profile", "status", "row_count", "built_by", "created_at",
                    "download")
    list_filter = ("organisation", "status", "target_system")
    readonly_fields = ("download",)

    @admin.display(description="File")
    def download(self, obj):
        if not obj.pk or not obj.content:
            return "–"
        return format_html('<a href="{}">Download</a>', reverse("console:file_download", args=[obj.pk]))

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
