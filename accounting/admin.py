from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html, format_html_join, linebreaks

from .models import BEHAVIOUR_GUIDE, Accrual, AccrualProfile, Behaviour, Cogs, Prepayment
from .scan import copy_history, default_window, scan

PROFILE_COLUMNS = ("name_link", "months", "typical", "lag", "confidence_pct", "memo_text", "rule_display",
                   "behaviour", "is_active")


class BehaviourInline(admin.TabularInline):
    """One section of the accrual page: the profiles iLA learned that behave one way."""

    model = AccrualProfile
    behaviour = None
    fields = PROFILE_COLUMNS
    readonly_fields = PROFILE_COLUMNS[:-2]
    extra = 0
    can_delete = False

    def get_queryset(self, request):
        return super().get_queryset(request).filter(behaviour=self.behaviour).select_related("account", "supplier")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Profile")
    def name_link(self, obj):
        url = reverse("admin:accounting_accrualprofile_change", args=[obj.pk])
        flag = "" if obj.is_current else " (not recent)"
        return format_html('<a href="{}">{}</a>{}', url, obj.name, flag)

    @admin.display(description="Months")
    def months(self, obj):
        if not obj.first_month:
            return ""
        return f"{obj.months_accrued} ({obj.first_month:%b %y}–{obj.last_month:%b %y})"

    @admin.display(description="Average / last")
    def typical(self, obj):
        return f"{obj.average:,.0f} / {obj.last_amount:,.0f}"

    @admin.display(description="Bill lag (days)")
    def lag(self, obj):
        return "" if obj.billing_lag_days is None else f"{obj.billing_lag_days:g}"

    @admin.display(description="Confidence")
    def confidence_pct(self, obj):
        return f"{obj.confidence:.0%}"

    @admin.display(description="Memo")
    def memo_text(self, obj):
        return format_html('<div style="max-width:32em;white-space:normal">{}</div>', obj.memo)

    @admin.display(description="Rule")
    def rule_display(self, obj):
        return format_html('<div style="max-width:24em;white-space:normal">{}</div>', obj.rule_text)


def _section(behaviour):
    examples, how = BEHAVIOUR_GUIDE[behaviour]
    return type(f"{behaviour.name.title()}Inline", (BehaviourInline,), {
        "behaviour": behaviour.value,
        "verbose_name": behaviour.label,
        "verbose_name_plural": f"{behaviour.label} — e.g. {examples.lower()} · how to accrue: {how.lower()}",
    })


@admin.register(Accrual)
class AccrualAdmin(admin.ModelAdmin):
    """An organisation's accruals. Scanning reads the accrual account's history and sorts what was accrued into
    the five sections below, each profile with a memo of what iLA found and the rule it would follow."""

    list_display = ("organisation", "account_codes", "history_months", "last_scanned_at", "profile_counts")
    list_filter = ("organisation",)
    actions = ["scan_history", "copy_and_scan"]
    readonly_fields = ("last_scanned_at", "scanned_from", "scanned_to", "summary")
    filter_horizontal = ("driver_categories",)
    fieldsets = (
        (None, {"fields": ("organisation", "account_codes", "history_months", "materiality", "driver_categories")}),
        ("Last scan", {"fields": ("last_scanned_at", "scanned_from", "scanned_to", "summary"),
                       "description": "Run a scan from the Accruals list: tick the organisation and choose an "
                                      "action. Your changes to a profile's behaviour, its notes and Active are "
                                      "kept when it is scanned again."}),
    )
    inlines = [_section(b) for b in Behaviour]

    def get_inlines(self, request, obj):
        return self.inlines if obj is not None and obj.pk else []

    @admin.display(description="Profiles")
    def profile_counts(self, obj):
        counts = (obj.scan_summary or {}).get("by_behaviour", {})
        return ", ".join(f"{label} {n}" for label, n in counts.items() if n) or "Not scanned"

    @admin.display(description="Summary")
    def summary(self, obj):
        s = obj.scan_summary or {}
        if not s:
            return "Not scanned yet."
        rows = [("Accrual accounts", ", ".join(s.get("accounts", []))),
                ("Journals touching them", s.get("journals", 0)),
                ("Lines on the accrual accounts", s.get("accrual_lines", 0)),
                ("Balance sheet lines skipped (reclasses)", s.get("balance_sheet_lines", 0)),
                ("Total accrued in the window", f"{s.get('accrued_total', 0):,.2f}"),
                ("Profiles", s.get("profiles", 0))]
        rows += [(f"— {label}", n) for label, n in s.get("by_behaviour", {}).items()]
        return format_html("<table>{}</table>", format_html_join("", "<tr><td>{}</td><td>{}</td></tr>", rows))

    def _run(self, request, queryset, *, copy):
        for accrual in queryset:
            start, end = default_window(accrual.history_months)
            try:
                if copy:
                    from xero.source import source_for

                    copied = copy_history(accrual, source_for(accrual.organisation), start, end)
                    self.message_user(request, f"{accrual.organisation}: {copied.total} transactions read from Xero "
                                               f"({copied.added} new, {copied.updated} changed).")
                result = scan(accrual, start, end)
            except Exception as exc:  # shown to the admin rather than a server error
                self.message_user(request, f"{accrual.organisation}: {exc}", messages.ERROR)
                continue
            self.message_user(request, f"{accrual.organisation}: {len(result.profiles)} profiles learned from "
                                       f"{start:%b %Y} to {end:%b %Y}.")

    @admin.action(description="Scan accrual history (transactions already in iLA)")
    def scan_history(self, request, queryset):
        self._run(request, queryset, copy=False)

    @admin.action(description="Copy history from Xero, then scan")
    def copy_and_scan(self, request, queryset):
        self._run(request, queryset, copy=True)


@admin.register(AccrualProfile)
class AccrualProfileAdmin(admin.ModelAdmin):
    """A single profile in full, with the postings it was learned from. Reached from its accrual's page."""

    list_display = ("name", "behaviour", "average", "confidence", "is_current", "is_active", "accrual")
    list_filter = ("accrual", "behaviour", "is_active", "is_current")
    search_fields = ("name", "memo")
    readonly_fields = ("accrual", "name", "detected_behaviour", "supplier", "supplier_found_by", "account",
                       "tracking_names", "driver", "months_accrued", "first_month", "last_month", "average",
                       "last_amount", "billing_lag_days", "lag_std", "accuracy_mape", "confidence", "is_current",
                       "memo_html", "rule_text", "rule", "amount_model", "evidence_table")
    fieldsets = (
        (None, {"fields": ("accrual", "name", "behaviour", "detected_behaviour", "is_active", "notes")}),
        ("What iLA found", {"fields": ("memo_html", "rule_text", "supplier", "supplier_found_by", "account",
                                       "tracking_names", "driver", "months_accrued", "first_month", "last_month",
                                       "average", "last_amount", "billing_lag_days", "lag_std", "accuracy_mape",
                                       "confidence", "is_current")}),
        ("Detail", {"fields": ("rule", "amount_model", "evidence_table"), "classes": ("collapse",)}),
    )

    def get_model_perms(self, request):
        return {}  # kept off the admin index; open profiles from their accrual

    def has_add_permission(self, request):
        return False

    @admin.display(description="Tracking")
    def tracking_names(self, obj):
        return ", ".join(str(o) for o in obj.tracking.all()) or "—"

    @admin.display(description="Memo")
    def memo_html(self, obj):
        return format_html("{}", linebreaks(obj.memo))

    @admin.display(description="Learned from")
    def evidence_table(self, obj):
        rows = ((e["date"], e["reference"], f"{e['amount']:,.2f}", e["description"]) for e in obj.evidence)
        return format_html("<table><tr><th>Date</th><th>Journal</th><th>Amount</th><th>Description</th></tr>{}"
                           "</table>", format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                                                        rows))


@admin.register(Prepayment)
class PrepaymentAdmin(admin.ModelAdmin):
    """Coming next: prepayments and amortisation (the accrual scan's periodic costs feed this)."""

    list_display = ("organisation", "account_codes")
    exclude = ("source_system", "source_id", "source_updated_at", "source_hash")


@admin.register(Cogs)
class CogsAdmin(admin.ModelAdmin):
    """To come: cost of goods sold."""

    list_display = ("organisation", "account_codes")
    exclude = ("source_system", "source_id", "source_updated_at", "source_hash")
