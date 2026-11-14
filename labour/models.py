"""The labour ledger: pay runs read from the payroll system's files, one line per employee, cost account and pay
component, with the days worked. Review, accruals and event tagging all read from it. See docs/Labour — Design.md.

Only the employee's code and name are kept: the payroll file's other personal fields (date of birth, hire date,
super fund and so on) are never read.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import MONEY, Dimension, DimensionValue, Journal, TenantModel


class LabourSettings(TenantModel):
    """How one organisation's payroll is read and journalled. Made with sensible guesses on the first upload."""

    class Source(models.TextChoices):
        MICROPAY = "micropay", "Micropay employee breakdown (xlsx)"

    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MICROPAY)
    journal_match = models.CharField(
        "payroll journal contains", max_length=100, default="FNE", blank=True,
        help_text="Text in the narration of the payroll journals posted in Xero, e.g. FNE for \"Ville for TECC "
                  "Payroll Journal FNE: 18/10/2026\". iLA finds a pay run's journal by this and its end date.")
    narration = models.CharField(
        max_length=255, default="Payroll Journal FNE: {period_end}",
        help_text="Narration for the journals iLA builds. {period_end} becomes the pay run's end date (1/11/2026).")
    credit_account = models.CharField(max_length=20, blank=True, help_text="e.g. 2350 Payroll Clearing.")
    tax_rate = models.CharField(max_length=50, default="BAS Excluded", help_text="Tax rate on the journal lines.")
    department_tracking = models.CharField(
        max_length=100, blank=True,
        help_text="Xero tracking category for departments, e.g. Departments (TrackingName1 on the journal).")
    event_dimension = models.ForeignKey(
        Dimension, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="The dimension labour is shared out by, e.g. Event (TrackingName2 on the journal).")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation"], name="labour_settings_one_per_org")]
        verbose_name = verbose_name_plural = "Labour settings"

    def __str__(self):
        return f"Labour settings: {self.organisation}"


class OnCost(TenantModel):
    """A cost on top of the pay run, as a rate of its total, e.g. payroll tax 4.95% to 6335."""

    settings = models.ForeignKey(LabourSettings, on_delete=models.CASCADE, related_name="on_costs")
    name = models.CharField(max_length=100)
    rate = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True,
                               help_text="As a fraction: 0.0495 is 4.95%. Blank: iLA learns it from the payroll "
                                         "journals in Xero on the next upload.")
    account = models.CharField(max_length=20, help_text="GL account code, e.g. 6335.")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(TenantModel.Meta):
        ordering = ["order", "name"]
        verbose_name = "on-cost"

    def __str__(self):
        return self.name


class PayComponent(TenantModel):
    """A pay component in the payroll system (Micropay's Pay Comp/Add Ded Code, e.g. Normal, SAT50, Super) and the
    GL account its pay goes to. New ones are added here when a pay-run file has them, to be given a GL account."""

    code = models.CharField(max_length=50)
    description = models.CharField(max_length=255, blank=True, help_text="e.g. Saturday Loading - 50%.")
    transaction_type = models.CharField(max_length=100, blank=True, help_text="As the last file had it, e.g. Super.")
    gl_account = models.CharField("GL account", max_length=20, blank=True, help_text="e.g. 6345.")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "code"], name="labour_paycomponent_code_uniq")]
        ordering = ["code"]
        verbose_name = "pay component"

    def __str__(self):
        return f"{self.code} {self.description}".strip()


class CostAccount(TenantModel):
    """A payroll cost account (e.g. 700-3000 TECC - F&B Service): its Xero department and whether its cost can go
    to an event. (The GL account comes from the pay component.) New ones appear here on upload, to be filled in."""

    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True,
                                  help_text="The option in Xero's department tracking, e.g. Food and Beverage "
                                            "Services.")
    taggable = models.BooleanField("can go to an event", default=True,
                                   help_text="Untick for costs never shared out by event, e.g. Fixed Wages or Event "
                                             "Sales: they stay Unassigned.")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "code"], name="labour_costaccount_code_uniq")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}".strip()

    @property
    def needs_mapping(self):
        return not self.department

    def clean(self):
        super().clean()
        if self.department:
            from .ledger import department_options

            options = department_options(self.organisation)
            if options and self.department not in options:
                raise ValidationError({"department": "Choose one of Xero's departments: " + ", ".join(options) + "."})


class Employee(TenantModel):
    """Who: the payroll system's employee code and name only."""

    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255, blank=True)
    employment_type = models.CharField(max_length=100, blank=True, help_text="e.g. CA - Casual.")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "code"], name="labour_employee_code_uniq")]
        ordering = ["name", "code"]

    def __str__(self):
        return f"{self.name} ({self.code})" if self.name else self.code


class PayRun(TenantModel):
    """One pay run, from one payroll file. Uploading a file for the same pay run again replaces its lines."""

    pay_period_id = models.CharField(max_length=50, blank=True)
    period_start = models.DateField(help_text="First day worked in the file.")
    period_end = models.DateField()
    pay_date = models.DateField(null=True, blank=True)
    frequency = models.CharField(max_length=50, blank=True)
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64)
    total = models.DecimalField(help_text="Sum of the file's lines (excludes on-costs).", **MONEY)
    summary_total = models.DecimalField(null=True, blank=True,
                                        help_text="The Grand Total on the file's Summary sheet, if it has one.",
                                        **MONEY)
    line_count = models.PositiveIntegerField(default=0)
    employee_count = models.PositiveIntegerField(default=0)
    xero_journal = models.ForeignKey(Journal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                     help_text="The payroll journal posted in Xero for this pay run, once found.")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "period_end"], name="labour_payrun_end_uniq")]
        ordering = ["-period_end"]

    def __str__(self):
        return f"Pay run ending {self.period_end:%d/%m/%Y}"


class PayLine(TenantModel):
    """One line of a pay run: an employee's pay of one component on one cost account, over the days it covers."""

    run = models.ForeignKey(PayRun, on_delete=models.CASCADE, related_name="lines")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="lines")
    cost_account = models.ForeignKey(CostAccount, on_delete=models.PROTECT, related_name="lines")
    pay_component = models.ForeignKey(PayComponent, on_delete=models.PROTECT, related_name="lines")
    transaction_type = models.CharField(max_length=100, blank=True, help_text="e.g. Hours By Rate, Super.")
    component_code = models.CharField(max_length=50, blank=True)
    component = models.CharField(max_length=255, blank=True, help_text="e.g. Saturday Loading - 50%.")
    date_from = models.DateField()
    date_to = models.DateField()
    hours = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(**MONEY)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["run", "employee"])]
        ordering = ["run", "employee", "date_from", "pk"]

    def __str__(self):
        return f"{self.employee} {self.component} {self.amount}"


class LabourOverride(TenantModel):
    """A person's decision on which event labour belongs to, over one or more days: for one employee, or for
    everyone working those days (employee blank). Value blank means Unassigned. It beats iLA's own allocation; the
    newest decision wins where two overlap. Only cost accounts that can go to an event are affected."""

    employee = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.CASCADE, related_name="overrides",
                                 help_text="Blank: everyone working those days.")
    date_from = models.DateField()
    date_to = models.DateField()
    value = models.ForeignKey(DimensionValue, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
                              help_text="The event. Blank: Unassigned.")
    note = models.CharField(max_length=255, blank=True)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.CheckConstraint(condition=models.Q(date_to__gte=models.F("date_from")),
                                   name="labour_override_dates")]
        ordering = ["-created_at", "-pk"]
        verbose_name = "labour change"

    def __str__(self):
        who = str(self.employee) if self.employee_id else "Everyone"
        days = f"{self.date_from:%d/%m/%Y}" + (f"–{self.date_to:%d/%m/%Y}" if self.date_to != self.date_from else "")
        return f"{who}, {days}: {self.value.code if self.value_id else 'Unassigned'}"


class LabourView(PayRun):
    """Admin entry for the Labour view (by employee, day and event)."""

    class Meta:
        proxy = True
        verbose_name = verbose_name_plural = "Labour view"
