"""Admin for open-item reconciliation: ledgers (with their categories), descriptions and the transaction register.
Categories and descriptions can be changed here as well as from the console; a rebuild keeps the changes."""

from datetime import date

from django.contrib import admin, messages
from django.db.models import Sum
from django.urls import reverse
from django.utils.html import format_html

from .models import OpenItem, OpenItemCategory, OpenItemLedger, OpenItemPosting
from .openitems import month_ends_between, natural_sign, report

SOURCE_FIELDS = ("source_system", "source_id", "source_updated_at", "source_hash")
EDITABLE = ("category", "description", "counterparty", "reference", "service_period")


def _xero_link(obj):
    return format_html('<a href="{}" target="_blank">Open</a>', obj.xero_url) if obj.xero_url else ""


class OpenItemCategoryInline(admin.TabularInline):
    model = OpenItemCategory
    fields = ("order", "code", "name", "behaviour", "standing", "accounts", "words", "scope", "is_active")
    extra = 1


@admin.register(OpenItemLedger)
class OpenItemLedgerAdmin(admin.ModelAdmin):
    """An account reconciled as open items: where it starts, how it was seeded, its categories and the last build."""

    list_display = ("account", "opening_date", "seed", "built_to", "job_status", "descriptions", "organisation")
    list_filter = ("organisation", "seed", "job_status")
    readonly_fields = ("account", "opening_date", "opening_balance", "seed", "seed_source", "gl_source", "built_to",
                       "job_status", "job_message", "job_finished_at", "descriptions")
    fields = ("account", "opening_date", "opening_balance", "seed", "seed_source", "lookback_months", "gl_source",
              "built_to", "job_status", "job_message", "job_finished_at", "descriptions")
    inlines = [OpenItemCategoryInline]
    actions = ["rebuild"]

    def has_add_permission(self, request):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """The page opens with the reconciliation at a month end (?at=YYYY-MM-DD; default the last one built)."""
        extra = dict(extra_context or {})
        ledger = OpenItemLedger.objects.filter(pk=object_id).select_related("account").first()
        if ledger is not None and ledger.built_to:
            days = month_ends_between(ledger.start, ledger.built_to)
            try:
                at = date.fromisoformat(request.GET.get("at", ""))
            except ValueError:
                at = None
            at = at if at in days else (days[-1] if days else None)
            if at is not None:
                data = report(ledger, at)
                extra.update(recon=data, recon_at=at, recon_days=days,
                             recon_difference=(data["xero"] - data["total"]) if data["xero"] is not None else None)
        elif ledger is not None:
            extra["recon_message"] = ("Not built yet" + (f": {ledger.job_message}" if ledger.job_message else ".")
                                      + " Run /reconcile in the console, or use the Rebuild action.")
        return super().change_view(request, object_id, form_url, extra)

    def save_formset(self, request, form, formset, change):
        for row in formset.save(commit=False):
            row.organisation = form.instance.organisation
            row.save()
        for row in formset.deleted_objects:
            row.delete()

    @admin.display(description="Descriptions")
    def descriptions(self, obj):
        url = reverse("admin:accounting_openitem_changelist") + f"?ledger__id__exact={obj.pk}"
        return format_html('<a href="{}">{} descriptions</a>', url, obj.items.count())

    @admin.action(description="Rebuild from iLA's copy (keeps edits and decisions)")
    def rebuild(self, request, queryset):
        from xero.source import source_for

        from . import openitem_jobs

        for ledger in queryset:
            to = ledger.built_to or ledger.opening_date
            if openitem_jobs.run(ledger, to, source_factory=source_for, copy=False):
                self.message_user(request, f"{ledger.account}: rebuilding to {to:%d %b %Y}.")
            else:
                self.message_user(request, f"{ledger.account}: a build is already running.", messages.WARNING)


@admin.register(OpenItemCategory)
class OpenItemCategoryAdmin(admin.ModelAdmin):
    """The accountant's categories. Changes to words or accounts sort the postings again at the next rebuild."""

    list_display = ("code", "name", "behaviour", "standing", "accounts", "words", "is_active", "ledger")
    list_editable = ("name", "behaviour", "standing", "accounts", "words", "is_active")
    list_filter = ("ledger__account", "behaviour", "is_active")
    search_fields = ("code", "name", "words", "accounts")
    exclude = SOURCE_FIELDS + ("organisation",)
    readonly_fields = ("ledger",)

    def has_add_permission(self, request):
        return False  # add them on the ledger's page, so they belong to its organisation


class OpenItemPostingInline(admin.TabularInline):
    model = OpenItemPosting
    fields = ("txn", "date", "movement", "amount", "record", "record_number", "lines", "from_lines", "basis",
              "confidence", "xero")
    readonly_fields = fields
    extra = 0
    can_delete = False
    ordering = ("seq",)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Open in Xero")
    def xero(self, obj):
        return _xero_link(obj)


@admin.register(OpenItem)
class OpenItemAdmin(admin.ModelAdmin):
    """Descriptions: one per accrual, invoice or prepaid item. Description, category, counterparty, reference,
    service period and note can be changed; a rebuild keeps those changes."""

    list_display = ("code", "category", "description", "counterparty", "reference", "raised_on", "original_amount",
                    "balance", "origin", "is_active", "ledger")
    list_editable = ("category", "description", "counterparty", "reference")
    list_filter = ("ledger__account", "origin", "category", "is_active")
    search_fields = ("code", "description", "counterparty", "reference")
    readonly_fields = ("code", "origin", "raised_on", "original_amount", "raised_by", "refs", "bill", "edited")
    fields = ("code", "category", "description", "counterparty", "reference", "service_period", "note", "origin",
              "raised_on", "original_amount", "raised_by", "refs", "bill", "edited", "is_active")
    inlines = [OpenItemPostingInline]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("ledger__account").annotate(
            balance_now=Sum("postings__amount"))

    @admin.display(description="Balance (as the balance sheet shows it)", ordering="balance_now")
    def balance(self, obj):
        if obj.balance_now is None:
            return "-"
        return f"{obj.balance_now * natural_sign(obj.ledger.account):,.2f}"

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "category":
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            item = OpenItem.objects.filter(pk=object_id).first() if object_id else None
            if item is not None:
                kwargs["queryset"] = OpenItemCategory.objects.filter(ledger=item.ledger)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if change:
            obj.edited = sorted(set(obj.edited or []) | {f for f in form.changed_data if f in EDITABLE})
        super().save_model(request, obj, form, change)

    def get_changelist_formset(self, request, **kwargs):
        formset = super().get_changelist_formset(request, **kwargs)

        class Tracked(formset):  # edits made in the list are kept by rebuilds too
            def save(self, commit=True):
                for form in self.forms:
                    if form.instance.pk and form.has_changed():
                        form.instance.edited = sorted(set(form.instance.edited or [])
                                                      | {f for f in form.changed_data if f in EDITABLE})
                return super().save(commit)

        return Tracked


@admin.register(OpenItemPosting)
class OpenItemPostingAdmin(admin.ModelAdmin):
    """The transaction register: every posting in an open-item account, its description, its Xero record and
    line(s), and how it was linked. Rebuilt on every run; links are changed from the console (move, confirm)."""

    list_display = ("txn", "date", "movement", "amount", "item", "record", "record_number", "lines", "from_lines",
                    "basis", "confidence", "xero", "ledger")
    list_filter = ("ledger__account", "movement", "confidence", "record")
    search_fields = ("txn", "item__code", "record_number", "record_id", "narration")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Open in Xero")
    def xero(self, obj):
        return _xero_link(obj)
