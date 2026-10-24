from django.contrib import admin, messages
from django.forms.models import BaseInlineFormSet
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
    list_display = ("name", "base_currency", "timezone", "chart_of_accounts")
    inlines = [InvoiceSetupInline]
    actions = ["refresh_chart_of_accounts"]

    @admin.display(description="Chart of accounts")
    def chart_of_accounts(self, obj):
        count = models.Account.objects.filter(organisation=obj, is_active=True).count()
        url = reverse("admin:core_account_changelist") + f"?organisation__id__exact={obj.pk}"
        return format_html('<a href="{}">{} accounts</a>', url, count)

    @admin.action(description="Refresh chart of accounts from Xero")
    def refresh_chart_of_accounts(self, request, queryset):
        from xero.api import XeroAPIError
        from xero.source import source_for
        from xero.sync import refresh_accounts

        for org in queryset:
            try:
                result = refresh_accounts(org, source_for(org), fresh=True)
            except XeroAPIError as exc:
                self.message_user(request, f"{org}: {exc}", messages.ERROR)
                continue
            models.AuditLog.objects.create(organisation=org, user=request.user, action=models.AuditLog.Action.SYNC,
                                           input="refresh chart of accounts",
                                           result={"added": result.added, "updated": result.updated,
                                                   "archived": result.archived})
            self.message_user(request, f"{org}: {len(result.rows)} accounts from Xero; {result.added} added, "
                                       f"{result.updated} updated, {result.archived} no longer in Xero. "
                                       "Reconciliation settings were kept.")


@admin.register(models.Account)
class AccountAdmin(admin.ModelAdmin):
    """The chart of accounts per organisation, loaded from Xero when it connects and refreshed from Organisations.
    Admins preset how each balance sheet account is reconciled and from what balance it is material."""

    list_display = ("code", "name", "account_class", "source_type", "recon_method", "recon_threshold", "is_active",
                    "organisation")
    list_display_links = ("code", "name")
    list_editable = ("recon_method", "recon_threshold")
    list_filter = ("organisation", "account_class", "recon_method", "is_active")
    search_fields = ("code", "name")
    ordering = ("organisation", "code")
    list_per_page = 500
    readonly_fields = ("organisation", "source_system", "source_id", "code", "name", "account_class", "source_type",
                       "system_account", "is_bank_account", "tax_code", "currency_code", "is_active")
    fieldsets = ((None, {"fields": readonly_fields}),
                 ("Month-end reconciliation", {"fields": ("recon_method", "recon_threshold")}))

    def has_add_permission(self, request):
        return False


@admin.register(models.Reconciliation)
class ReconciliationAdmin(admin.ModelAdmin):
    list_display = ("account", "period_end", "method", "gl_balance", "support_balance", "difference", "prepared_by",
                    "reviewed_by", "organisation")
    list_filter = ("organisation", "period_end", "method")
    search_fields = ("account__code", "account__name")
    readonly_fields = ("organisation", "account", "period_end", "method", "gl_balance", "support_balance", "items",
                       "notes", "prepared_by", "prepared_at", "reviewed_by", "reviewed_at")
    exclude = ("source_system", "source_id", "source_updated_at", "source_hash")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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
    models.Contact,
    models.TrackingCategory,
    models.TrackingOption,
    models.Document,
    models.DocumentLine,
    models.BankTransaction,
    models.BankTransactionLine,
    models.Journal,
    models.JournalLine,
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
    """Every tagging decision: approved in /tag (method Manual) or copied from Xero tracking (method Tracking)."""

    list_display = ("transaction", "date", "contact", "values", "method", "status", "rule", "set_by", "set_at")
    list_filter = ("organisation", "dimension", "method", "status", "level")
    search_fields = ("document__number", "document__reference", "document__contact__name",
                     "bank_transaction__reference", "bank_transaction__contact__name", "journal__narration",
                     "shares__dimension_value__code", "shares__dimension_value__name", "rule")
    inlines = [AssignmentShareInline]
    list_select_related = ("document__contact", "bank_transaction__contact", "journal", "set_by")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("shares__dimension_value")

    @admin.display(description="Transaction")
    def transaction(self, obj):
        return str(obj.target)

    @admin.display(description="Date")
    def date(self, obj):
        parent = obj.target
        parent = getattr(parent, "document", None) or getattr(parent, "bank_transaction", None) or \
            getattr(parent, "journal", None) or parent
        return getattr(parent, "date", None)

    @admin.display(description="Contact")
    def contact(self, obj):
        parent = obj.document or obj.bank_transaction
        return parent.contact if parent is not None and parent.contact_id else ""

    @admin.display(description="Tagged to")
    def values(self, obj):
        shares = list(obj.shares.all())
        if not shares:
            return "Unassigned"
        return ", ".join(s.dimension_value.code + ("" if s.allocation == 1 else f" {s.allocation:.0%}")
                         for s in shares)


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organisation", "user", "action", "succeeded", "input")
    list_filter = ("organisation", "action", "succeeded")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DimensionValueFormSet(BaseInlineFormSet):
    def clean(self):
        """No two values on the page may share a code, ignoring case (the database checks saved ones)."""
        super().clean()
        seen = {}
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            code = (form.cleaned_data.get("code") or "").strip()
            if not code:
                continue
            if code.lower() in seen:
                form.add_error("code", f"Code {code} is already used by {seen[code.lower()]} on this page.")
            else:
                seen[code.lower()] = form.cleaned_data.get("name") or code


class DimensionValueInline(admin.TabularInline):
    model = models.DimensionValue
    formset = DimensionValueFormSet
    extra = 0
    fields = ("code", "name", "description", "tracking_option", "attributes", "is_active")
    raw_id_fields = ("tracking_option",)


@admin.register(models.Dimension)
class DimensionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organisation", "replaces_source", "source_name", "is_active")
    list_filter = ("organisation", "replaces_source", "is_active")
    search_fields = ("name", "code", "source_name")
    fieldsets = (
        (None, {"fields": ("organisation", "code", "name", "description", "is_active")}),
        ("Accounting system", {
            "fields": ("replaces_source", "source_name", "tracking_category"),
            "description": "Fill these in when this dimension replaces one the accounting system already has, so "
                           "iLA shows your names instead of the system's codes. The accounting system isn't changed.",
        }),
        ("Extra fields for values", {"fields": ("attribute_fields",), "classes": ("collapse",)}),
    )
    raw_id_fields = ("tracking_category",)
    inlines = [DimensionValueInline]

    def save_formset(self, request, form, formset, change):
        """Inline values belong to the dimension's organisation."""
        for value in formset.save(commit=False):
            value.organisation = form.instance.organisation
            value.save()
        for value in formset.deleted_objects:
            value.delete()
        formset.save_m2m()

