"""Admin for the month-end review: settings (the accounts in each group), review requests with their trial run and
confirmation, and the review points (notes and sign-offs) people left in /review. The core checks' materiality is
kept in the database for later but not shown while /review shows every movement."""

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.utils.html import format_html, format_html_join

from accounting import jobs

from . import ratios
from .models import RequestStatus, ReviewFinding, ReviewRatio, ReviewRequest, ReviewSettings
from .requests import SpecError, confirm, describe, trial_run

SOURCE_FIELDS = ("source_system", "source_id", "source_updated_at", "source_hash")


def can_confirm(user):
    return user.is_superuser or user.has_perm("review.confirm_reviewrequest")


class ReviewRatioInline(admin.TabularInline):
    """The ratios under the P&L in /review, in order. Formulas are numerator / denominator, e.g.
    5110-5330 / 4005 + 4810 + 4830 or lab / rev."""

    model = ReviewRatio
    fields = ("order", "name", "formula", "enabled")
    extra = 1

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        organisation = obj.organisation if obj else None

        class Formset(formset):
            def _construct_form(self, i, **kwargs):
                form = super()._construct_form(i, **kwargs)
                if organisation is not None:
                    form.instance.organisation = organisation  # so clean() checks codes against its chart
                return form

        return Formset


@admin.register(ReviewSettings)
class ReviewSettingsAdmin(admin.ModelAdmin):
    """Which accounts make up Cost of sales, Labour, Depreciation & amortisation, Interest and Income tax in /review
    (cost accounts in none of them are Other operating costs), and the ratios under the P&L. New settings start with
    the default ratios (gross profit, COGS, labour, EBITDA and net profit % of revenue)."""

    list_display = ("organisation", "labour_accounts", "trial_months", "pack_view")
    fields = ("organisation", "cogs_accounts", "labour_accounts", "interest_accounts", "tax_accounts",
              "depreciation_accounts", "trial_months", "pack_view")
    inlines = [ReviewRatioInline]

    def save_formset(self, request, form, formset, change):
        for item in formset.save(commit=False):
            item.organisation = form.instance.organisation
            item.save()
        for item in formset.deleted_objects:
            item.delete()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change and not form.instance.ratios.exists():
            ReviewRatio.objects.bulk_create([
                ReviewRatio(organisation=form.instance.organisation, settings=form.instance, name=name,
                            formula=formula, order=i) for i, (name, formula) in enumerate(ratios.DEFAULTS, 1)])


@admin.register(ReviewRequest)
class ReviewRequestAdmin(admin.ModelAdmin):
    """Write the request, save, then Trial run: iLA works out the check and shows its result for the last months.
    Reword and trial run again until it's right; then an admin confirms it and /review runs it every month."""

    list_display = ("code", "title", "status", "enabled", "runs_now", "trial_at", "confirmed_at", "organisation")
    list_filter = ("organisation", "status", "enabled")
    readonly_fields = ("code", "status", "job", "check_plain", "check_parts", "cannot", "trial_table", "trial_info",
                       "confirmed_info", "spec", "confirmed_spec", "history_table")
    change_form_template = "admin/review/reviewrequest/change_form.html"

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("organisation", "title", "prompt", "trial_months", "enabled")
        return ("code", "organisation", "title", "prompt", "trial_months", "enabled", "status", "job", "check_plain",
                "check_parts", "cannot", "trial_info", "trial_table", "confirmed_info", "spec", "confirmed_spec",
                "history_table")

    @admin.display(boolean=True, description="Runs in /review")
    def runs_now(self, obj):
        return obj.runs

    @admin.display(description="Progress")
    def job(self, obj):
        if not obj.job_status:
            return "Not trial run yet."
        return f"{obj.job_status.capitalize()}: {obj.job_message}"

    @admin.display(description="The check iLA worked out")
    def check_plain(self, obj):
        return obj.plain or "—"

    @admin.display(description="Its building blocks")
    def check_parts(self, obj):
        return describe(obj.spec) if obj.spec else "—"

    @admin.display(description="Trial run")
    def trial_info(self, obj):
        if not obj.trial_at:
            return "—"
        months = ", ".join(obj.trial_result.get("months", []))
        changed = " (the request has changed since: trial run it again)" if obj.trial_prompt != obj.prompt else ""
        return f"{obj.trial_at:%d %b %Y %H:%M} by {obj.trial_by or '?'} on {months}{changed}"

    @admin.display(description="Trial result (as it will show in /review)")
    def trial_table(self, obj):
        result = obj.trial_result or {}
        if not result.get("columns"):
            return "—"
        if not result.get("rows"):
            return "Nothing to raise in these months."
        head = format_html_join("", "<th>{}</th>", ((c[0],) for c in result["columns"]))
        body = format_html_join("", "<tr>{}</tr>", ((format_html_join(
            "", '<td style="{}">{}</td>', ((("text-align:right" if c[1] in ("money", "number", "pct") else ""),
                                            _show(v, c[1])) for v, c in zip(row, result["columns"]))),)
            for row in result["rows"]))
        total = result.get("total")
        foot = format_html("<p>{} row(s){}</p>", result.get("count", 0),
                           f", total {float(total):,.2f}" if total else "")
        return format_html("<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>{}", head, body, foot)

    @admin.display(description="Confirmed")
    def confirmed_info(self, obj):
        if not obj.confirmed_at:
            return "Not confirmed yet."
        note = "" if obj.status == RequestStatus.CONFIRMED else " · the latest wording isn't confirmed yet; " \
                                                                 "/review runs the confirmed check until it is"
        return f"{obj.confirmed_at:%d %b %Y %H:%M} by {obj.confirmed_by or '?'}: {obj.confirmed_plain}{note}"

    @admin.display(description="History")
    def history_table(self, obj):
        if not obj.history:
            return "—"
        return format_html("<ul>{}</ul>", format_html_join("", "<li>{} {}: {} — {}</li>", (
            (h.get("at", "")[:16].replace("T", " "), h.get("by", ""), h.get("what", ""), h.get("prompt", ""))
            for h in reversed(obj.history))))

    def save_model(self, request, obj, form, change):
        if change and "prompt" in form.changed_data and obj.status != RequestStatus.DRAFT:
            obj.status = RequestStatus.DRAFT
            messages.info(request, "The request changed: trial run it, then confirm."
                          + (" Until then /review keeps running the confirmed check." if obj.confirmed_spec else ""))
        super().save_model(request, obj, form, change)

    def response_change(self, request, obj):
        if "_trial" in request.POST:
            if trial_run(obj, request.user):
                messages.info(request, "Trial run started: it reads Xero and works out the check (under a minute). "
                                       "Reload this page to see the result.")
            else:
                messages.warning(request, "A trial run is already going for this request.")
            return HttpResponseRedirect(request.path)
        if "_confirm" in request.POST:
            if not can_confirm(request.user):
                messages.error(request, "Only an admin can confirm review requests.")
            else:
                try:
                    confirm(obj, request.user)
                    messages.success(request, f"{obj.code} confirmed: /review runs it from now on.")
                except SpecError as exc:
                    messages.error(request, str(exc))
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)

    def render_change_form(self, request, context, *args, obj=None, **kwargs):
        context["can_confirm"] = can_confirm(request.user)
        context["running"] = bool(obj and obj.job_status == jobs.RUNNING)
        return super().render_change_form(request, context, *args, obj=obj, **kwargs)


def _show(value, kind):
    if value in (None, ""):
        return ""
    if kind == "money":
        return f"{float(value):,.2f}"
    if kind == "pct":
        return f"{float(value) * 100:.1f}%"
    return value


@admin.register(ReviewFinding)
class ReviewFindingAdmin(admin.ModelAdmin):
    """Notes and sign-offs left in /review, one per month and reference (account code, group or RR-01)."""

    list_display = ("period_end", "key", "label", "status", "signed", "note", "updated_by", "updated_at",
                    "organisation")
    list_filter = ("organisation", "period_end", "status")
    list_editable = ("note",)
    search_fields = ("key", "label", "note")
    fields = ("organisation", "period_end", "key", "label", "status", "note", "signed_amount", "signed_at",
              "signed_by", "history_table", "updated_by")
    readonly_fields = ("organisation", "period_end", "key", "label", "status", "signed_amount", "signed_at",
                       "signed_by", "history_table", "updated_by")

    def has_add_permission(self, request):
        return False

    @admin.display(description="Signed off")
    def signed(self, obj):
        if obj.status != "signed_off":
            return "—"
        return f"{obj.signed_by or '?'} {obj.signed_at:%d %b %Y}, at {obj.signed_amount:,.2f}"

    @admin.display(description="History")
    def history_table(self, obj):
        if not obj.history:
            return "—"
        return format_html("<ul>{}</ul>", format_html_join("", "<li>{} {}: {} {}</li>", (
            (h.get("at", "")[:16].replace("T", " "), h.get("by", ""), h.get("what", ""),
             h.get("note") or h.get("amount") or h.get("refs") or "") for h in reversed(obj.history))))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        if "note" in form.changed_data:
            obj.log(request.user, "notes edited in admin", note=obj.note)
        super().save_model(request, obj, form, change)
