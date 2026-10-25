from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join, linebreaks
from django.utils.safestring import mark_safe

from . import jobs
from .balance import compare, open_accruals
from .ledger import LedgerError, build_import_file, check_bills, mark_posted, run_accruals
from .models import (BEHAVIOUR_GUIDE, Accrual, AccrualProfile, Behaviour, Cogs, Expectation, LedgerJournal,
                     Prepayment, State)
from .util import month_end, month_start

SOURCE_FIELDS = ("source_system", "source_id", "source_updated_at", "source_hash")


def _wrap(text, width="24em"):
    return format_html('<div style="max-width:{};white-space:normal">{}</div>', width, text)


def _keep_changes(form):
    """A person changed a profile: remember which learned fields, so scans leave them alone."""
    if form.instance.pk and form.has_changed():
        form.instance.keep(form.changed_data)


class SameOrganisation:
    """Account, supplier and tracking choices come from the record's own organisation only."""

    parent_model_for_org = None

    def _organisation(self, request):
        object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
        model = self.parent_model_for_org or self.model
        row = model.objects.filter(pk=object_id).first() if object_id else None
        return row.organisation if row else None

    def _scope(self, db_field, request, kwargs):
        related = db_field.related_model
        if any(f.name == "organisation" for f in related._meta.fields):
            org = self._organisation(request)
            if org is not None:
                kwargs["queryset"] = related.objects.filter(organisation=org)
        return kwargs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name != "organisation":
            kwargs = self._scope(db_field, request, kwargs)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        return super().formfield_for_manytomany(db_field, request, **self._scope(db_field, request, kwargs))


# --- Accruals ---------------------------------------------------------------------------------------------------


class BehaviourInline(SameOrganisation, admin.TabularInline):
    """One section of the accrual page: the profiles iLA learned that behave one way. Everything you can change
    here, and more on the profile's own page; deleting a profile keeps it out of later scans."""

    model = AccrualProfile
    parent_model_for_org = Accrual
    behaviour = None
    columns = ("name_link", "months", "typical", "confidence_pct", "memo_text", "rule_display")
    editable = ("behaviour", "method", "amount", "is_active")
    extra = 0
    can_delete = True

    def get_fields(self, request, obj=None):
        return self.columns + self.editable

    def get_readonly_fields(self, request, obj=None):
        return self.columns

    def get_queryset(self, request):
        return super().get_queryset(request).filter(behaviour=self.behaviour).select_related("account", "supplier")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Profile")
    def name_link(self, obj):
        url = reverse("admin:accounting_accrualprofile_change", args=[obj.pk])
        flags = ("" if obj.is_current else " (not recent)") + (" ✎" if obj.kept_fields else "")
        return format_html('<a href="{}">{}</a>{}', url, obj.name, flags)

    @admin.display(description="Months")
    def months(self, obj):
        if not obj.first_month:
            return ""
        return f"{obj.months_accrued} ({obj.first_month:%b %y}–{obj.last_month:%b %y})"

    @admin.display(description="Average / last")
    def typical(self, obj):
        return f"{obj.average:,.0f} / {obj.last_amount:,.0f}"

    @admin.display(description="Confidence")
    def confidence_pct(self, obj):
        return f"{obj.confidence:.0%}"

    @admin.display(description="Memo")
    def memo_text(self, obj):
        return _wrap(obj.memo, "30em")

    @admin.display(description="Rule")
    def rule_display(self, obj):
        return _wrap(obj.rule_text)


def _section(behaviour):
    examples, how = BEHAVIOUR_GUIDE[behaviour]
    attrs = {"behaviour": behaviour.value, "verbose_name": behaviour.label,
             "verbose_name_plural": f"{behaviour.label} — e.g. {examples.lower()} · how to accrue: {how.lower()}"}
    if behaviour == Behaviour.DRIVER:
        attrs["editable"] = ("behaviour", "method", "driver", "driver_rate", "amount", "is_active")
    return type(f"{behaviour.name.title()}Inline", (BehaviourInline,), attrs)


@admin.register(Accrual)
class AccrualAdmin(SameOrganisation, admin.ModelAdmin):
    """An organisation's accruals. Scanning reads the accrual account's history and sorts what was accrued into
    the five sections below, each profile with a memo of what iLA found and the rule it would follow. Running
    saves a journal in the Expectation Ledger."""

    list_display = ("organisation", "account_codes", "history_months", "materiality", "last_job",
                    "last_scanned_at", "profile_counts", "journals_link")
    list_filter = ("organisation",)
    actions = ["scan_history", "copy_and_scan", "run_last_month"]
    readonly_fields = ("last_job", "last_scanned_at", "scanned_from", "scanned_to", "summary")
    filter_horizontal = ("driver_categories",)
    fieldsets = (
        (None, {"fields": ("organisation", "account_codes", "history_months", "materiality", "driver_categories")}),
        ("Thresholds", {
            "fields": ("fixed_variation", "recurring_coverage", "bill_tolerance", "bill_window_days",
                       "dormant_months", "min_driver_options", "trailing_months", "driver_words", "ignore_words"),
            "description": "How the scan decides. Change them and scan again: profiles are re-sorted, except "
                           "fields you have changed on a profile."}),
        ("Journals", {"fields": ("journal_tax_rate", "date_format")}),
        ("Deleted profiles", {"fields": ("excluded_keys",), "classes": ("collapse",)}),
        ("Last scan", {"fields": ("last_job", "last_scanned_at", "scanned_from", "scanned_to", "summary"),
                       "description": "Scan or run from the Accruals list: tick the organisation and choose an "
                                      "action. Fields you change on a profile (marked ✎), its notes and Active are "
                                      "kept when it is scanned again; a profile you delete is not brought back."}),
    )
    inlines = [_section(b) for b in Behaviour]

    def get_inlines(self, request, obj):
        return self.inlines if obj is not None and obj.pk else []

    def save_formset(self, request, form, formset, change):
        if formset.model is AccrualProfile:
            for f in formset.forms:
                if f not in formset.deleted_forms:
                    _keep_changes(f)
        super().save_formset(request, form, formset, change)

    @admin.display(description="Profiles")
    def profile_counts(self, obj):
        counts = (obj.scan_summary or {}).get("by_behaviour", {})
        return ", ".join(f"{label} {n}" for label, n in counts.items() if n) or "Not scanned"

    @admin.display(description="Expectation Ledger")
    def journals_link(self, obj):
        url = reverse("admin:accounting_ledgerjournal_changelist") + f"?accrual__id__exact={obj.pk}"
        return format_html('<a href="{}">{} journals</a>', url, obj.journals.count())

    @admin.display(description="Summary")
    def summary(self, obj):
        s = obj.scan_summary or {}
        if not s:
            return "Not scanned yet."
        rows = [("Accrual accounts", ", ".join(s.get("accounts", []))),
                ("Journals touching them", s.get("journals", 0)),
                ("Lines on the accrual accounts", s.get("accrual_lines", 0)),
                ("Balance sheet lines skipped (reclasses)", s.get("balance_sheet_lines", 0)),
                ("Left out because you deleted them", s.get("deleted_by_you", 0)),
                ("Total accrued in the window", f"{s.get('accrued_total', 0):,.2f}"),
                ("Profiles", s.get("profiles", 0))]
        rows += [(f"— {label}", n) for label, n in s.get("by_behaviour", {}).items()]
        return format_html("<table>{}</table>", format_html_join("", "<tr><td>{}</td><td>{}</td></tr>", rows))

    @admin.display(description="Last job")
    def last_job(self, obj):
        if not obj.job_status:
            return "–"
        when = obj.job_finished_at or obj.job_started_at
        stamp = f"{timezone.localtime(when):%d %b %H:%M}" if when else ""
        if obj.job_status == jobs.RUNNING:
            if obj.job_started_at and timezone.now() - obj.job_started_at > jobs.STALE_AFTER:
                return f"Stopped without finishing (started {stamp}); start it again. {obj.job_message}"
            return f"Running since {stamp}: {obj.job_message} Refresh the page to follow it."
        return f"{obj.get_job_status_display()} {stamp}: {obj.job_message}"

    def _scan(self, request, queryset, *, copy):
        # In the background: copying from Xero takes minutes and the web server stops a request after 30 seconds.
        for accrual in queryset:
            if jobs.copy_and_scan(accrual, copy=copy, user=request.user):
                self.message_user(request, f"{accrual.organisation}: started "
                                           + ("copying from Xero, then scanning" if copy else "scanning")
                                           + ". It takes a few minutes; refresh this page and see Last job.")
            else:
                self.message_user(request, f"{accrual.organisation}: a job is already running; see Last job.",
                                  messages.WARNING)

    @admin.action(description="Scan accrual history (transactions already in iLA)")
    def scan_history(self, request, queryset):
        self._scan(request, queryset, copy=False)

    @admin.action(description="Copy history from Xero, then scan")
    def copy_and_scan(self, request, queryset):
        self._scan(request, queryset, copy=True)

    @admin.action(description="Run accruals for last month (saves an Expectation Ledger journal)")
    def run_last_month(self, request, queryset):
        period_end = month_start(timezone.localdate()) - timezone.timedelta(days=1)
        for accrual in queryset:
            try:
                journal = run_accruals(accrual, period_end, request.user)
            except LedgerError as exc:
                self.message_user(request, f"{accrual.organisation}: {exc}", messages.ERROR)
                continue
            url = reverse("admin:accounting_ledgerjournal_change", args=[journal.pk])
            self.message_user(request, format_html(
                '{}: <a href="{}">{}</a> saved with {} lines ({} left out).', accrual.organisation, url, journal,
                journal.lines.count(), len(journal.skipped)))


@admin.register(AccrualProfile)
class AccrualProfileAdmin(SameOrganisation, admin.ModelAdmin):
    """A single profile: change anything (the rule, the driver, the account, the memo), clear the driver or rate,
    or delete the profile. Your changes are listed under 'Your changes' and a scan leaves them alone."""

    list_display = ("name", "accrual", "behaviour", "method", "amount", "driver", "driver_rate", "confidence",
                    "is_current", "is_active", "edited")
    list_editable = ("behaviour", "method", "amount", "is_active")
    list_filter = ("accrual", "behaviour", "method", "is_active", "is_current")
    search_fields = ("name", "memo", "notes")
    actions = ["relearn"]
    filter_horizontal = ("tracking",)
    readonly_fields = ("accrual", "key", "detected_behaviour", "supplier_found_by", "rule_text", "months_accrued",
                       "first_month", "last_month", "average", "last_amount", "lag_std", "accuracy_mape",
                       "confidence", "is_current", "rule", "amount_model", "outcome_table", "evidence_table")
    fieldsets = (
        (None, {"fields": ("accrual", "name", "behaviour", "detected_behaviour", "is_active", "notes",
                           "kept_fields")}),
        ("Rule (what a run does)", {"fields": ("rule_text", "method", "amount", "account", "tracking", "supplier",
                                               "supplier_found_by", "driver", "driver_category", "driver_rate",
                                               "seasonality", "reverse_next_month", "skip_if_bill_posted",
                                               "billing_lag_days")}),
        ("What iLA found", {"fields": ("memo", "months_accrued", "first_month", "last_month", "average",
                                       "last_amount", "lag_std", "accuracy_mape", "confidence", "is_current")}),
        ("Detail", {"fields": ("rule", "amount_model", "outcome_table", "evidence_table", "key"),
                    "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):  # also used by the list's editable columns
        _keep_changes(form)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.save(update_fields=["rule_text"])  # tracking is saved after the profile

    @admin.display(description="Edited", boolean=True)
    def edited(self, obj):
        return bool(obj.kept_fields)

    @admin.action(description="Let the next scan relearn the fields I changed")
    def relearn(self, request, queryset):
        n = queryset.exclude(kept_fields=[]).update(kept_fields=[])
        self.message_user(request, f"{n} profiles will be relearned on the next scan.")

    @admin.display(description="Run outcomes")
    def outcome_table(self, obj):
        if not obj.outcomes:
            return "None yet: closed Expectation Ledger lines appear here."
        rows = ((o["period"], o["expected"], o["actual"], f"{o['error']:.1%}") for o in obj.outcomes)
        return format_html("<table><tr><th>Period</th><th>Expected</th><th>Actual</th><th>Gap</th></tr>{}</table>",
                           format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows))

    @admin.display(description="Learned from")
    def evidence_table(self, obj):
        rows = ((e["date"], e["reference"], f"{e['amount']:,.2f}", e["description"]) for e in obj.evidence)
        return format_html("<table><tr><th>Date</th><th>Journal</th><th>Amount</th><th>Description</th></tr>{}"
                           "</table>", format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                                                        rows))


# --- Expectation Ledger -----------------------------------------------------------------------------------------


def _history_html(entries, last=None):
    entries = entries[-last:] if last else entries
    rows = ((e.get("at", "")[:16].replace("T", " "), e.get("by", ""),
             f"{e['from']} → {e['to']}" if "to" in e else "", e.get("why", "")) for e in entries)
    return format_html('<table style="font-size:11px">{}</table>',
                       format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows))


class ExpectationForm(forms.ModelForm):
    why = forms.CharField(required=False, label="Why (for the history)",
                          widget=forms.TextInput(attrs={"size": 18}))

    class Meta:
        model = Expectation
        fields = ("description", "account", "expected_amount", "reverse_next_month", "state")


class ExpectationInline(SameOrganisation, admin.TabularInline):
    """The journal's lines. Add a line, change an amount or state, or delete a line; each change is written to the
    line's history with your name and the reason you give."""

    model = Expectation
    parent_model_for_org = LedgerJournal
    form = ExpectationForm
    fields = ("description", "account", "expected_amount", "reverse_next_month", "state", "why", "basis_text",
              "evidence_text", "actual_amount", "variance", "history_text")
    readonly_fields = ("basis_text", "evidence_text", "actual_amount", "variance", "history_text")
    extra = 0
    show_change_link = True

    @admin.display(description="Basis")
    def basis_text(self, obj):
        return _wrap(obj.basis or ("added by a person" if obj.is_manual else ""), "12em")

    @admin.display(description="Evidence")
    def evidence_text(self, obj):
        return _wrap("; ".join(f"{e.get('type', '')} {e.get('number', '')} {e.get('date', '')} {e.get('amount', '')}"
                               .strip() for e in obj.evidence) or "—", "14em")

    @admin.display(description="History (latest)")
    def history_text(self, obj):
        return _history_html(obj.history, last=2) if obj.history else "—"


class LedgerJournalForm(forms.ModelForm):
    class Meta:
        model = LedgerJournal
        fields = "__all__"

    def clean(self):
        data = super().clean()
        if self.instance.pk is None and data.get("accrual") and data.get("period_end"):
            existing = LedgerJournal.objects.filter(accrual=data["accrual"], kind=data.get("kind", "accrual"),
                                                    period_end=month_end(data["period_end"])).first()
            if existing:
                raise forms.ValidationError(f"{existing} already exists. Open it and use 'Run again' to refresh "
                                            "its lines.")
        return data


@admin.register(LedgerJournal)
class LedgerJournalAdmin(SameOrganisation, admin.ModelAdmin):
    """Each accrual run, saved as a journal. Lines go predicted → evidenced → posted → closed; the journal shows
    the least advanced. Add one by choosing the accrual set-up and the month end: iLA fills in the lines."""

    list_display = ("__str__", "organisation", "period_end", "state", "line_count", "expected_total",
                    "actual_total", "created_by", "file_link", "posted_by")
    list_filter = ("organisation", "accrual", "state", "period_end")
    form = LedgerJournalForm
    actions = ["rerun", "find_bills", "build_file", "posted"]
    inlines = [ExpectationInline]
    readonly_fields = ("organisation", "state", "skipped_table", "file_link", "created_by", "posted_by", "posted_at",
                       "lifecycle", "compare_table", "open_table")
    fieldsets = (
        (None, {"fields": ("accrual", "kind", "period_end", "narration", "state", "lifecycle", "notes")}),
        ("Left out of this run", {"fields": ("skipped_table",)}),
        ("Compared with Xero", {
            "fields": ("compare_table",),
            "description": "What this journal expects against what was accrued in Xero for the month (manual "
                           "journals crediting the accrual account, reversals left out). Xero's accruals are never "
                           "an input to the expectation: this is only the comparison."}),
        ("Accruals still open in Xero", {
            "fields": ("open_table",), "classes": ("collapse",),
            "description": "Every accrual on the accrual account not yet reversed or cleared at the month end, from "
                           "the history copied into iLA, with whether its invoice has arrived."}),
        ("Posting", {"fields": ("file_link", "created_by", "posted_by", "posted_at")}),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        return fields + ("accrual", "kind", "period_end") if obj else fields

    def get_inlines(self, request, obj):
        return self.inlines if obj is not None and obj.pk else []

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        obj.organisation = obj.accrual.organisation
        obj.period_end = month_end(obj.period_end)
        try:
            run_accruals(obj.accrual, obj.period_end, request.user, journal=obj)
        except LedgerError as exc:
            self.message_user(request, str(exc), messages.ERROR)

    def save_formset(self, request, form, formset, change):
        journal = form.instance
        for f in formset.forms:
            if f in formset.deleted_forms or not f.has_changed():
                continue
            line, why = f.instance, f.cleaned_data.get("why") or "Changed in admin"
            changed = [name for name in f.changed_data if name != "why"]
            if not changed:
                continue
            line.organisation, line.is_manual = journal.organisation, True
            if line.pk is None:
                line.log(request.user, f"Added in admin: {why}", amount=line.expected_amount)
                continue
            before = {name: f.initial.get(name) for name in changed}
            new_state = f.cleaned_data.get("state")
            if "state" in changed:
                line.state = before.pop("state")
                line.move(new_state, request.user, why,
                          **{f"{k}_before": v for k, v in before.items()})
            else:
                line.log(request.user, why, **{f"{k}_before": v for k, v in before.items()})
        deleted = [f.instance for f in formset.deleted_forms if f.instance.pk]
        if deleted:
            stamp = f"{timezone.localtime():%d %b %Y %H:%M} {request.user.get_username()}"
            journal.notes = "\n".join(filter(None, [journal.notes] + [
                f"{stamp} deleted line: {line.description} {line.expected_amount} ({line.get_state_display()})"
                for line in deleted]))
            journal.save(update_fields=["notes", "updated_at"])
        super().save_formset(request, form, formset, change)
        journal.refresh_state()

    @admin.display(description="Lines")
    def line_count(self, obj):
        return obj.lines.count()

    @admin.display(description="Expected")
    def expected_total(self, obj):
        return f"{obj.total_expected:,.2f}"

    @admin.display(description="Actual")
    def actual_total(self, obj):
        actual = [line.actual_amount for line in obj.lines.all() if line.actual_amount is not None]
        return f"{sum(actual):,.2f}" if actual else "—"

    @admin.display(description="Import file")
    def file_link(self, obj):
        if not obj.import_file_id:
            return "—"
        url = reverse("admin:core_importfile_change", args=[obj.import_file_id])
        return format_html('<a href="{}">{}</a> ({})', url, obj.import_file.file_name,
                           obj.import_file.get_status_display())

    @admin.display(description="Lines by state")
    def lifecycle(self, obj):
        counts = {s.label: obj.lines.filter(state=s.value).count() for s in State}
        return " → ".join(f"{label} {n}" for label, n in counts.items())

    @admin.display(description="Skipped")
    def skipped_table(self, obj):
        if not obj.skipped:
            return "Nothing."
        ordered = sorted(obj.skipped, key=lambda s: (not s.get("review"), s.get("profile", "")))
        rows = ((s.get("profile", ""), "To review" if s.get("review") else "", s.get("why", "")) for s in ordered)
        return format_html("<table>{}</table>",
                           format_html_join("", "<tr><td>{}</td><td><strong>{}</strong></td><td>{}</td></tr>", rows))

    @admin.display(description="By account")
    def compare_table(self, obj):
        if not obj.pk:
            return "–"
        rows = compare(obj)
        if not rows:
            return "Nothing expected and nothing accrued in Xero."
        body = [(f"{a.code} {a.name}", f"{e:,.2f}", f"{x:,.2f}", f"{d:,.2f}") for a, e, x, d in rows]
        totals = [sum(r[i] for r in rows) for i in (1, 2, 3)]
        body.append(("Total", *(f"{t:,.2f}" for t in totals)))
        head = mark_safe("<tr><th>Account</th><th>Expected</th><th>Accrued in Xero</th><th>Difference</th></tr>")
        return format_html("<table>{}{}</table>", head, format_html_join(
            "", '<tr><td>{}</td><td style="text-align:right">{}</td><td style="text-align:right">{}</td>'
                '<td style="text-align:right">{}</td></tr>', body))

    @admin.display(description="Open at the month end")
    def open_table(self, obj):
        if not obj.pk:
            return "–"
        still, first = open_accruals(obj.accrual, obj.period_end)
        if not still:
            return "Nothing open."
        total = sum((a.open for a in still), Decimal("0"))
        body = [(f"{a.date:%d %b %Y}", a.narration[:90], f"{a.open:,.2f}", a.status(obj.period_end)) for a in still]
        body.append(("", "Total of what was read", f"{total:,.2f}", ""))
        note = (f"Read from {first:%d %b %Y}. A debit that matches no accrual is usually the reversal of one from "
                "before then; copy more history to pair them up. The total differs from Xero's balance by "
                "whatever was open before the history began." if first else "")
        return format_html("<p>{}</p><table>{}</table>", note, format_html_join(
            "", '<tr><td>{}</td><td>{}</td><td style="text-align:right">{}</td><td>{}</td></tr>', body))

    def _each(self, request, queryset, do, done):
        for journal in queryset:
            try:
                result = do(journal)
            except (LedgerError, Exception) as exc:  # shown to the admin rather than a server error
                self.message_user(request, f"{journal}: {exc}", messages.ERROR)
                continue
            self.message_user(request, f"{journal}: {done(journal, result)}")

    @admin.action(description="Run again (refresh lines not yet posted; your lines are kept)")
    def rerun(self, request, queryset):
        self._each(request, queryset, lambda j: run_accruals(j.accrual, j.period_end, request.user, journal=j),
                   lambda j, r: f"{j.lines.count()} lines, {len(r.skipped)} left out.")

    @admin.action(description="Check for bills (evidence predicted lines, close posted lines)")
    def find_bills(self, request, queryset):
        self._each(request, queryset, lambda j: check_bills(j, request.user),
                   lambda j, n: f"{n} lines moved on; now {j.get_state_display().lower()}.")

    @admin.action(description="Build and stamp the journal import file")
    def build_file(self, request, queryset):
        self._each(request, queryset, lambda j: build_import_file(j, request.user),
                   lambda j, f: f"{f.file_name} built and stamped ({f.row_count} rows).")

    @admin.action(description="Mark posted in Xero")
    def posted(self, request, queryset):
        self._each(request, queryset, lambda j: mark_posted(j, request.user),
                   lambda j, r: f"posted; lines now {j.get_state_display().lower()}.")


@admin.register(Expectation)
class ExpectationAdmin(SameOrganisation, admin.ModelAdmin):
    """One expectation in full, with its whole history. Reached from its journal."""

    list_display = ("description", "journal", "state", "expected_amount", "actual_amount", "variance")
    list_filter = ("journal__accrual", "state")
    readonly_fields = ("journal", "profile", "basis", "confidence", "evidence", "full_history", "actual_amount",
                       "variance", "is_manual")
    exclude = SOURCE_FIELDS + ("history", "organisation")
    filter_horizontal = ("tracking",)

    def get_model_perms(self, request):
        return {}  # kept off the admin index; open lines from their journal

    def save_model(self, request, obj, form, change):
        changed = [n for n in form.changed_data]
        if changed:
            obj.is_manual = True
            before = {f"{n}_before": form.initial.get(n) for n in changed if n != "state"}
            if "state" in changed:
                new = obj.state
                obj.state = form.initial.get("state")
                obj.move(new, request.user, "Changed in admin", **before)
            else:
                obj.log(request.user, "Changed in admin", **before)
        super().save_model(request, obj, form, change)
        obj.journal.refresh_state()

    @admin.display(description="History")
    def full_history(self, obj):
        return _history_html(obj.history)


@admin.register(Prepayment)
class PrepaymentAdmin(admin.ModelAdmin):
    """Coming next: prepayments and amortisation (the accrual scan's periodic costs feed this)."""

    list_display = ("organisation", "account_codes")
    exclude = SOURCE_FIELDS


@admin.register(Cogs)
class CogsAdmin(admin.ModelAdmin):
    """To come: cost of goods sold."""

    list_display = ("organisation", "account_codes")
    exclude = SOURCE_FIELDS
