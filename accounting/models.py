"""Month-end accounting work iLA does itself: accruals first, then prepayments and COGS.

Everything here sits beside the source ledger and never changes it. An organisation's accrual set-up says which
balance sheet account holds its accruals (TECC: 2000); scanning that account's history learns one profile per
recurring cost, each with a memo (what iLA found) and a rule (how iLA would accrue it). See
docs/Expectation Ledger — Design.md.
"""

from django.db import models

from core.models import MONEY, Account, Contact, TenantModel, TrackingCategory, TrackingOption


class Behaviour(models.TextChoices):
    """How a cost behaves, which decides how it is accrued."""

    FIXED = "fixed", "Fixed recurring"
    VARIABLE = "variable", "Variable recurring"
    DRIVER = "driver", "Driver-linked"
    PERIODIC = "periodic", "Periodic, non-monthly"
    ONE_OFF = "one_off", "One-off"


# Shown with each section in admin: examples and how it is accrued.
BEHAVIOUR_GUIDE = {
    Behaviour.FIXED: ("Software subscriptions, leases, contracted cleaning", "Exact amount"),
    Behaviour.VARIABLE: ("Electricity, water, gas", "Run-rate × seasonality"),
    Behaviour.DRIVER: ("Security per event, laundry per room-night, casual labour per cover",
                       "Driver volume × learned rate"),
    Behaviour.PERIODIC: ("Quarterly rates, annual insurance", "Prepayment or amortisation, not accrual"),
    Behaviour.ONE_OFF: ("Repairs, consultants", "Only accrue from evidence (cut-off scan, PO, user input)"),
}


class Accrual(TenantModel):
    """An organisation's accrual set-up and the result of its last history scan."""

    account_codes = models.CharField(
        "accrual accounts", max_length=255, default="2000",
        help_text="Code(s) of the balance sheet account(s) accruals are posted to, comma-separated, e.g. 2000.")
    history_months = models.PositiveSmallIntegerField(
        default=24, help_text="How many months of history to learn from, ending with the last full month.")
    materiality = models.DecimalField(
        "materiality floor", max_digits=14, decimal_places=2, default=500,
        help_text="Accruals below this are left out of the rules (profiles are still learned).")
    driver_categories = models.ManyToManyField(
        TrackingCategory, blank=True, related_name="+", verbose_name="driver tracking categories",
        help_text="Tracking categories that count activity, e.g. Event. Their options are drivers, not locations, "
                  "so one profile can cover many events. Blank: any category whose name mentions an event.")
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    scanned_from = models.DateField(null=True, blank=True)
    scanned_to = models.DateField(null=True, blank=True)
    scan_summary = models.JSONField(default=dict, blank=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation"], name="accounting_accrual_one_per_org")]
        verbose_name = "accrual"
        verbose_name_plural = "accruals"

    def __str__(self):
        return f"{self.organisation} accruals ({self.account_codes})"

    @property
    def codes(self):
        return [c.strip() for c in self.account_codes.split(",") if c.strip()]

    def accounts(self):
        return Account.objects.filter(organisation=self.organisation, code__in=self.codes)


class AccrualProfile(TenantModel):
    """One recurring cost learned from the accrual history: supplier (or description) × expense account ×
    location/department tracking. Driver tracking such as events is not part of the key; it is what the cost is
    driven by."""

    accrual = models.ForeignKey(Accrual, on_delete=models.CASCADE, related_name="profiles")
    key = models.CharField(max_length=255, help_text="What the scan groups by; stable across scans.")
    name = models.CharField(max_length=255)
    behaviour = models.CharField(
        max_length=20, choices=Behaviour.choices,
        help_text="Change it if iLA got it wrong; a later scan keeps your choice.")
    detected_behaviour = models.CharField(max_length=20, choices=Behaviour.choices)
    supplier = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    supplier_found_by = models.CharField(max_length=20, blank=True, help_text="description or bills")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    tracking = models.ManyToManyField(TrackingOption, blank=True, related_name="+")
    driver = models.CharField(max_length=100, blank=True, help_text="e.g. Event, when the cost follows activity.")

    months_accrued = models.PositiveSmallIntegerField(default=0)
    first_month = models.DateField(null=True, blank=True)
    last_month = models.DateField(null=True, blank=True)
    average = models.DecimalField(default=0, **MONEY)
    last_amount = models.DecimalField(default=0, **MONEY)
    amount_model = models.JSONField(default=dict, blank=True,
                                    help_text="Statistics: median, min, max, variation, seasonality, driver rate.")
    billing_lag_days = models.FloatField(null=True, blank=True, help_text="Month end → bill date, median.")
    lag_std = models.FloatField(null=True, blank=True)
    accuracy_mape = models.FloatField(null=True, blank=True,
                                      help_text="Average gap between what was accrued and the bill.")
    confidence = models.FloatField(default=0)
    is_current = models.BooleanField(default=True, help_text="Accrued in the last six months of the history.")

    memo = models.TextField(blank=True, help_text="What iLA found in the history.")
    rule = models.JSONField(default=dict, blank=True, help_text="How iLA would accrue it, as data.")
    rule_text = models.TextField("rule", blank=True)
    evidence = models.JSONField(default=list, blank=True, help_text="The postings the profile was learned from.")

    is_active = models.BooleanField(default=True, help_text="Untick to stop iLA proposing this accrual.")
    notes = models.TextField(blank=True, help_text="Reviewer notes, e.g. 'bills quarterly in arrears'.")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["accrual", "key"], name="accounting_accrualprofile_key_uniq")]
        ordering = ["-average"]
        verbose_name = "accrual profile"

    def __str__(self):
        return self.name

    @property
    def is_reviewed(self):
        return self.behaviour != self.detected_behaviour or bool(self.notes) or not self.is_active


class Prepayment(TenantModel):
    """Placeholder: prepayments and amortisation are next (periodic costs found by the accrual scan land here)."""

    account_codes = models.CharField("prepayment accounts", max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "prepayment"

    def __str__(self):
        return f"{self.organisation} prepayments"


class Cogs(TenantModel):
    """Placeholder: cost of goods sold (stocktakes, inventory movement) is to come."""

    account_codes = models.CharField("COGS accounts", max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta(TenantModel.Meta):
        verbose_name = "COGS"
        verbose_name_plural = "COGS"

    def __str__(self):
        return f"{self.organisation} COGS"
