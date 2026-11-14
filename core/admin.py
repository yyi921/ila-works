from django.contrib import admin, messages
from django.contrib.admin import helpers
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


class DatesFilter(admin.SimpleListFilter):
    title = "dates"
    parameter_name = "dates"

    def lookups(self, request, model_admin):
        return (("set", "Has dates"), ("missing", "No dates"))

    def queryset(self, request, queryset):
        if self.value() == "set":
            return queryset.filter(start_date__isnull=False)
        if self.value() == "missing":
            return queryset.filter(start_date__isnull=True)
        return queryset


@admin.register(models.DimensionValue)
class DimensionValueAdmin(admin.ModelAdmin):
    """Each value of a dimension (e.g. each event) on its own, with its dates. The dates and bump days can be
    changed straight from the list; Suggest dates fills in the ones that are missing, after you confirm."""

    list_display = ("code", "name", "dimension", "start_date", "end_date", "bump_in_days", "bump_out_days",
                    "season_shown", "in_xero", "is_active")
    list_editable = ("start_date", "end_date", "bump_in_days", "bump_out_days", "is_active")
    list_filter = ("organisation", "dimension", DatesFilter, "is_active")
    search_fields = ("code", "name", "description")
    list_select_related = ("dimension", "tracking_option")
    list_per_page = 100  # 100 rows of editable columns stay well under Django's 1,000-field limit
    ordering = ("dimension", "-start_date", "code")
    fields = ("dimension", "code", "name", "description", "tracking_option", "start_date", "end_date",
              "bump_in_days", "bump_out_days", "season", "attributes", "is_active")
    raw_id_fields = ("tracking_option",)
    actions = ["suggest_dates"]

    @admin.display(description="Season", boolean=True)
    def season_shown(self, obj):
        return obj.is_season

    @admin.display(description="In Xero", boolean=True)
    def in_xero(self, obj):
        return obj.tracking_option_id is not None

    def save_model(self, request, obj, form, change):
        """A value belongs to its dimension's organisation. Linking it to a Xero option tags the lines already
        copied with that option."""
        obj.organisation = obj.dimension.organisation
        super().save_model(request, obj, form, change)
        if "tracking_option" in form.changed_data and obj.tracking_option_id:
            from .tagging import reapply_tracking

            count = reapply_tracking(obj.dimension, [obj.tracking_option_id])
            if count:
                messages.info(request, f"Tags refreshed on {count} transaction(s) tracked to {obj.code} in Xero.")

    @admin.action(description="Suggest dates for the selected values that have none")
    def suggest_dates(self, request, queryset):
        from django.template.response import TemplateResponse

        from .event_dates import suggest_all

        values = queryset.select_related("dimension")
        if request.POST.get("apply"):
            chosen = set(request.POST.getlist("use"))
            done = 0
            for found in suggest_all(values):
                if str(found.value.pk) in chosen:
                    found.value.start_date, found.value.end_date = found.start, found.end
                    found.value.save(update_fields=["start_date", "end_date", "updated_at"])
                    done += 1
            messages.success(request, f"Dates saved on {done} value(s).")
            return None
        suggestions = suggest_all(values)
        if not suggestions:
            messages.warning(request, "No dates could be suggested for the selected values (those that already "
                                      "have dates are left alone). Type them in the list instead.")
            return None
        return TemplateResponse(request, "admin/core/dimensionvalue/suggest_dates.html", {
            **self.admin_site.each_context(request),
            "title": "Suggested dates",
            "opts": self.model._meta,
            "suggestions": suggestions,
            "selected": request.POST.getlist(helpers.ACTION_CHECKBOX_NAME),
            "select_across": request.POST.get("select_across", "0"),
            "without": values.filter(start_date__isnull=True).count() - len(suggestions),
        })


@admin.register(models.Dimension)
class DimensionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organisation", "values_link", "replaces_source", "source_name", "is_active")
    list_filter = ("organisation", "replaces_source", "is_active")
    search_fields = ("name", "code", "source_name")
    readonly_fields = ("values_link",)
    fieldsets = (
        (None, {"fields": ("organisation", "code", "name", "description", "is_active", "values_link")}),
        ("Dates", {
            "fields": ("bump_in_days", "bump_out_days", "season_days"),
            "description": "Defaults for values with dates, e.g. events: the set-up and pack-down days around each "
                           "one, and how long a value can run before it counts as a season. A value can override "
                           "its own bump days.",
        }),
        ("Accounting system", {
            "fields": ("replaces_source", "source_name", "tracking_category"),
            "description": "Fill these in when this dimension replaces one the accounting system already has, so "
                           "iLA shows your names instead of the system's codes. The accounting system isn't changed.",
        }),
        ("Extra fields for values", {"fields": ("attribute_fields",), "classes": ("collapse",)}),
    )
    raw_id_fields = ("tracking_category",)

    @admin.display(description="Values")
    def values_link(self, obj):
        if obj is None or obj.pk is None:
            return "Save the dimension first, then add its values."
        url = reverse("admin:core_dimensionvalue_changelist") + f"?dimension__id__exact={obj.pk}"
        missing = obj.values.filter(start_date__isnull=True).count()
        return format_html('<a href="{}">{} values</a> ({} without dates)', url, obj.values.count(), missing)

