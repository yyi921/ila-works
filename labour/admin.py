from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from core.models import Organisation

from . import ledger
from .mappings import COST_ACCOUNTS, PAY_COMPONENTS, import_csv, to_csv
from .micropay import FileError
from .models import CostAccount, Employee, LabourSettings, OnCost, PayComponent, PayLine, PayRun

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024


class OnCostInline(admin.TabularInline):
    model = OnCost
    fields = ("name", "rate", "account", "order")
    extra = 0


@admin.register(LabourSettings)
class LabourSettingsAdmin(admin.ModelAdmin):
    list_display = ("organisation", "source", "journal_match", "credit_account", "department_tracking",
                    "event_dimension")
    fields = ("organisation", "source", "journal_match", "narration", "credit_account", "tax_rate",
              "department_tracking", "event_dimension")
    inlines = [OnCostInline]

    def save_formset(self, request, form, formset, change):
        for obj in formset.save(commit=False):
            obj.organisation = form.instance.organisation
            obj.save()
        for obj in formset.deleted_objects:
            obj.delete()


class MissingFilter(admin.SimpleListFilter):
    """Rows still to set up: a cost account without a department, a pay component without a GL account."""

    title = "set-up"
    parameter_name = "mapping"
    field = ""
    label = ""

    def lookups(self, request, model_admin):
        return (("missing", self.label), ("done", "Set up"))

    def queryset(self, request, queryset):
        if self.value() == "missing":
            return queryset.filter(**{self.field: ""})
        if self.value() == "done":
            return queryset.exclude(**{self.field: ""})
        return queryset


class NoDepartmentFilter(MissingFilter):
    field, label = "department", "No department"


class NoGLFilter(MissingFilter):
    field, label = "gl_account", "No GL account"


class CsvFileForm(forms.Form):
    organisation = forms.ModelChoiceField(Organisation.objects.all())
    file = forms.FileField(help_text="A CSV with the columns shown above. Download the current list to start from.")


class CsvMixin:
    """Download as CSV and Upload (CSV) buttons on a mapping list."""

    spec = None
    csv_notes = ()
    change_list_template = "admin/labour/csv_change_list.html"

    def _url_name(self, action):
        return f"labour_{self.model._meta.model_name}_{action}"

    def get_urls(self):
        return [path("upload/", self.admin_site.admin_view(self.upload_view), name=self._url_name("upload")),
                path("download/", self.admin_site.admin_view(self.download_view),
                     name=self._url_name("download"))] + super().get_urls()

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
        return super().changelist_view(request, {
            **(extra_context or {}), "csv_upload_url": reverse(f"admin:{self._url_name('upload')}") + query,
            "csv_download_url": reverse(f"admin:{self._url_name('download')}") + query, "csv_what": self.spec.what})

    def download_view(self, request):
        org = self._organisation(request)
        if org is None:
            messages.warning(request, "Filter the list by organisation first, then download.")
            return redirect(f"admin:{self._url_name('changelist')}")
        response = HttpResponse(to_csv(self.spec, org), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{org.name} {self.spec.what}.csv"'
        return response

    def upload_view(self, request):
        if not request.user.has_perm(f"labour.change_{self.model._meta.model_name}"):
            messages.error(request, f"You don't have permission to change {self.spec.what}.")
            return redirect(f"admin:{self._url_name('changelist')}")
        org = self._organisation(request)
        form = CsvFileForm(request.POST or None, request.FILES or None, initial={"organisation": org} if org else None)
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
                    result = import_csv(self.spec, org, request.user, text, upload.name)
                    problems = result.problems
                    if not problems:
                        messages.success(request, f"{self.spec.what.capitalize()} from {upload.name}: "
                                                  f"{len(result.created)} new, {len(result.updated)} updated, "
                                                  f"{result.unchanged} unchanged.")
                        return redirect(reverse(f"admin:{self._url_name('changelist')}")
                                        + f"?organisation__id__exact={org.pk}")
        return TemplateResponse(request, "admin/labour/csv_upload.html", {
            **self.admin_site.each_context(request), "title": f"Upload {self.spec.what}", "form": form,
            "problems": problems, "headings": [h for h, _ in self.spec.headings], "opts": self.model._meta,
            "notes": self.csv_notes,
            "download_url": reverse(f"admin:{self._url_name('download')}") + (f"?organisation={org.pk}" if org else "")})


@admin.register(CostAccount)
class CostAccountAdmin(CsvMixin, admin.ModelAdmin):
    """Payroll cost accounts: the Xero department each goes to, and whether its cost can go to an event. New ones
    are added here when a pay-run file has them."""

    spec = COST_ACCOUNTS
    csv_notes = ("Department: one of Xero's departments (e.g. Food and Beverage Services).",
                 "Can go to an event: yes or no. No keeps its cost Unassigned (e.g. Fixed Wages, Event Sales).",
                 "The GL account comes from the pay component (Labour → Pay components), not the cost account.")
    list_display = ("code", "name", "department", "taggable", "organisation")
    list_editable = ("department", "taggable")
    list_filter = ("organisation", NoDepartmentFilter, "taggable")
    search_fields = ("code", "name", "department")
    fields = ("organisation", "code", "name", "department", "taggable")


@admin.register(PayComponent)
class PayComponentAdmin(CsvMixin, admin.ModelAdmin):
    """Pay components (Micropay's Pay Comp/Add Ded Code) and the GL account each goes to. New ones are added here
    when a pay-run file has them."""

    spec = PAY_COMPONENTS
    csv_notes = ("Code: the Pay Comp/Add Ded Code exactly as in Micropay (e.g. SAT50).",
                 "GL account: an account code in the chart (e.g. 6345).")
    list_display = ("code", "description", "transaction_type", "gl_account", "organisation")
    list_editable = ("gl_account",)
    list_filter = ("organisation", NoGLFilter, "transaction_type")
    search_fields = ("code", "description")
    fields = ("organisation", "code", "description", "transaction_type", "gl_account")
    readonly_fields = ("transaction_type",)


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
                cost_accounts=ledger.by_cost_account(run), components=ledger.by_component(run),
                gl_rows=sorted(gl.items()), components_url=reverse("admin:labour_paycomponent_changelist"),
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
            'New cost accounts: {}. <a href="{}">Give each a department and say whether it can go to an event</a>.',
            ", ".join(str(c) for c in result.new_cost_accounts), url))
    unset = [c for c in result.new_components if not c.gl_account]
    if unset:
        url = reverse("admin:labour_paycomponent_changelist") + "?mapping=missing"
        messages.warning(request, format_html(
            'New pay components without a GL account: {}. <a href="{}">Give each a GL account</a>.',
            ", ".join(c.code for c in unset), url))
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
