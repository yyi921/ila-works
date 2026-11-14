from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from core.models import Organisation

from . import ledger
from .cost_accounts import HEADINGS, import_csv, to_csv
from .micropay import FileError
from .models import ComponentRule, CostAccount, Employee, LabourSettings, OnCost, PayLine, PayRun

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024


class OnCostInline(admin.TabularInline):
    model = OnCost
    fields = ("name", "rate", "account", "order")
    extra = 0


class ComponentRuleInline(admin.TabularInline):
    model = ComponentRule
    fields = ("match_on", "text", "account", "order")
    extra = 0
    verbose_name_plural = "Pay component rules (first match wins; other pay goes to its cost account's GL)"


@admin.register(LabourSettings)
class LabourSettingsAdmin(admin.ModelAdmin):
    list_display = ("organisation", "source", "journal_match", "credit_account", "department_tracking",
                    "event_dimension")
    fields = ("organisation", "source", "journal_match", "narration", "credit_account", "tax_rate",
              "department_tracking", "event_dimension")
    inlines = [OnCostInline, ComponentRuleInline]

    def save_formset(self, request, form, formset, change):
        for obj in formset.save(commit=False):
            obj.organisation = form.instance.organisation
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()


class NeedsMappingFilter(admin.SimpleListFilter):
    title = "set-up"
    parameter_name = "mapping"

    def lookups(self, request, model_admin):
        return (("missing", "Department or GL missing"), ("done", "Set up"))

    def queryset(self, request, queryset):
        missing = queryset.filter(department="") | queryset.filter(gl_account="")
        if self.value() == "missing":
            return missing
        if self.value() == "done":
            return queryset.exclude(pk__in=missing)
        return queryset


@admin.register(CostAccount)
class CostAccountAdmin(admin.ModelAdmin):
    """Payroll cost accounts: which Xero department and GL account each goes to, and whether its cost can go to an
    event. New ones are added here when a pay-run file has them."""

    list_display = ("code", "name", "department", "gl_account", "taggable", "organisation")
    list_editable = ("department", "gl_account", "taggable")
    list_filter = ("organisation", NeedsMappingFilter, "taggable")
    search_fields = ("code", "name", "department")
    fields = ("organisation", "code", "name", "department", "gl_account", "taggable")
    change_list_template = "admin/labour/costaccount/change_list.html"

    def get_urls(self):
        return [path("upload/", self.admin_site.admin_view(self.upload_view), name="labour_costaccount_upload"),
                path("download/", self.admin_site.admin_view(self.download_view),
                     name="labour_costaccount_download")] + super().get_urls()

    def _organisation(self, request):
        """The organisation filtered on in the list, else the only one."""
        pk = request.GET.get("organisation") or request.GET.get("organisation__id__exact")
        if pk:
            return Organisation.objects.filter(pk=pk).first()
        organisations = Organisation.objects.all()
        return organisations.first() if organisations.count() == 1 else None

    def changelist_view(self, request, extra_context=None):
        org = self._organisation(request)
        query = f"?organisation={org.pk}" if org else ""
        return super().changelist_view(request, {**(extra_context or {}), "org_query": query})

    def download_view(self, request):
        org = self._organisation(request)
        if org is None:
            messages.warning(request, "Filter the list by organisation first, then download.")
            return redirect("admin:labour_costaccount_changelist")
        response = HttpResponse(to_csv(org), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{org.name} cost accounts.csv"'
        return response

    def upload_view(self, request):
        if not request.user.has_perm("labour.change_costaccount"):
            messages.error(request, "You don't have permission to change cost accounts.")
            return redirect("admin:labour_costaccount_changelist")
        org = self._organisation(request)
        form = CostAccountUploadForm(request.POST or None, request.FILES or None,
                                     initial={"organisation": org} if org else None)
        problems = []
        if request.method == "POST" and form.is_valid():
            upload, org = form.cleaned_data["file"], form.cleaned_data["organisation"]
            if not upload.name.lower().endswith(".csv"):
                form.add_error("file", f"{upload.name} isn't a CSV file. In Excel: File → Save As → CSV UTF-8.")
            elif upload.size > MAX_CSV_BYTES:
                form.add_error("file", "The file is over 1 MB.")
            else:
                try:
                    text = upload.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = None
                    form.add_error("file", "The file isn't saved as UTF-8. In Excel: File → Save As → CSV UTF-8.")
                if text is not None:
                    result = import_csv(org, request.user, text, upload.name)
                    problems = result.problems
                    if not problems:
                        messages.success(request, f"Cost accounts from {upload.name}: {len(result.created)} new, "
                                                  f"{len(result.updated)} updated, {result.unchanged} unchanged.")
                        return redirect(reverse("admin:labour_costaccount_changelist")
                                        + f"?organisation__id__exact={org.pk}")
        return TemplateResponse(request, "admin/labour/costaccount/upload.html", {
            **self.admin_site.each_context(request), "title": "Upload cost accounts", "form": form,
            "problems": problems, "headings": HEADINGS, "opts": self.model._meta,
            "download_url": reverse("admin:labour_costaccount_download") + (f"?organisation={org.pk}" if org else "")})


class CostAccountUploadForm(forms.Form):
    organisation = forms.ModelChoiceField(Organisation.objects.all())
    file = forms.FileField(help_text="A CSV with the columns Code, Name, Department, GL account and Can go to an "
                                     "event (yes/no). Download the current list to start from.")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "employment_type", "organisation")
    list_filter = ("organisation", "employment_type")
    search_fields = ("name", "code")
    readonly_fields = ("organisation", "code", "name", "employment_type")
    fields = readonly_fields

    def has_add_permission(self, request):
        return False


class UploadForm(forms.Form):
    organisation = forms.ModelChoiceField(Organisation.objects.all())
    file = forms.FileField(help_text="The Micropay employee breakdown for one pay run (.xlsx), as it came.")


@admin.register(PayRun)
class PayRunAdmin(admin.ModelAdmin):
    """Pay runs read from payroll files. Upload one with the button at the top right; the same pay run uploaded
    again replaces the earlier one."""

    list_display = ("period_end", "period_start", "pay_date", "employee_count", "line_count", "total_shown",
                    "in_xero", "file_name", "organisation")
    list_filter = ("organisation",)
    date_hierarchy = "period_end"
    change_list_template = "admin/labour/payrun/change_list.html"
    change_form_template = "admin/labour/payrun/change_form.html"
    readonly_fields = ("organisation", "pay_period_id", "period_start", "period_end", "pay_date", "frequency",
                       "file_name", "total", "summary_total", "line_count", "employee_count", "xero_journal",
                       "uploaded_by", "created_at", "updated_at")
    fields = readonly_fields

    @admin.display(description="Total", ordering="total")
    def total_shown(self, obj):
        return f"{obj.total:,.2f}"

    @admin.display(description="In Xero", boolean=True)
    def in_xero(self, obj):
        return obj.xero_journal_id is not None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [path("upload/", self.admin_site.admin_view(self.upload_view), name="labour_payrun_upload")] \
            + super().get_urls()

    def upload_view(self, request):
        if not request.user.has_perm("labour.add_payline") and not request.user.is_superuser:
            messages.error(request, "You don't have permission to upload pay runs.")
            return redirect("admin:labour_payrun_changelist")
        organisations = Organisation.objects.all()
        form = UploadForm(request.POST or None, request.FILES or None,
                          initial={"organisation": organisations.first()} if organisations.count() == 1 else None)
        if request.method == "POST" and form.is_valid():
            upload = form.cleaned_data["file"]
            if upload.size > MAX_UPLOAD_BYTES:
                form.add_error("file", "The file is over 10 MB.")
            else:
                try:
                    result = ledger.upload(form.cleaned_data["organisation"], request.user, upload.name,
                                           upload.read())
                except FileError as exc:
                    form.add_error("file", str(exc))
                else:
                    _report(request, result)
                    return redirect("admin:labour_payrun_change", result.run.pk)
        return TemplateResponse(request, "admin/labour/payrun/upload.html", {
            **self.admin_site.each_context(request), "title": "Upload a pay-run file", "form": form,
            "opts": self.model._meta})

    def change_view(self, request, object_id, form_url="", extra_context=None):
        run = self.get_object(request, object_id)
        extra = dict(extra_context or {})
        if run is not None:
            settings = ledger.settings_for(run.organisation)
            gl, on_costs = ledger.by_gl(run, settings)
            extra.update(
                cost_accounts=ledger.by_cost_account(run), gl_rows=sorted(gl.items()),
                on_costs=[(cost, amount, f"{cost.rate * 100:.3f}%" if cost.rate is not None else "")
                          for cost, amount in on_costs],
                comparison=ledger.xero_comparison(run, settings),
                lines_url=reverse("admin:labour_payline_changelist") + f"?run__id__exact={run.pk}",
                cost_accounts_url=reverse("admin:labour_costaccount_changelist"),
                settings_url=reverse("admin:labour_laboursettings_change", args=[settings.pk]))
        return super().change_view(request, object_id, form_url, extra_context=extra)


def _report(request, result):
    run = result.run
    if result.unchanged:
        messages.info(request, f"{run.file_name} was already uploaded for the pay run ending "
                               f"{run.period_end:%d/%m/%Y}; nothing changed.")
    else:
        messages.success(request, f"{'Replaced' if result.replaced else 'Saved'} the pay run ending "
                                  f"{run.period_end:%d/%m/%Y}: {run.employee_count} employees, {run.line_count} "
                                  f"lines, {run.total:,.2f}.")
    if result.new_cost_accounts:
        url = reverse("admin:labour_costaccount_changelist") + "?mapping=missing"
        messages.warning(request, format_html(
            'New cost accounts: {}. <a href="{}">Give each a department and GL account</a>.',
            ", ".join(str(c) for c in result.new_cost_accounts), url))
    for text in result.warnings + result.learned:
        messages.warning(request, text)


@admin.register(PayLine)
class PayLineAdmin(admin.ModelAdmin):
    list_display = ("employee", "cost_account", "transaction_type", "component", "date_from", "date_to", "hours",
                    "amount", "run")
    list_filter = ("organisation", "run", "cost_account", "transaction_type")
    search_fields = ("employee__name", "employee__code", "component")
    list_select_related = ("employee", "cost_account", "run")
    list_per_page = 200

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
