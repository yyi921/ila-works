"""Month-end accounting work iLA does itself: accruals first, then prepayments and COGS.

Everything here sits beside the source ledger and never changes it. An organisation's accrual set-up says which
balance sheet account holds its accruals (TECC: 2000) and the thresholds the scan uses; scanning that account's
history learns one profile per recurring cost, each with a memo (what iLA found) and a rule (how iLA would accrue
it). Every rule, threshold and profile can be changed or deleted in admin, and a re-scan keeps those changes.

Each accrual run is saved as a journal in the Expectation Ledger. Its lines move through
predicted → evidenced → posted → closed, and every change records who or what made it and why.
See docs/Expectation Ledger — Design.md.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import MONEY, Account, Contact, ImportFile, TenantModel, TrackingCategory, TrackingOption

DEFAULT_DRIVER_WORDS = ("event, events, function, functions, security, laundry, linen, room, rooms, covers, casual, "
                        "casuals, labour, labor, staffing, agency, hire")
DEFAULT_IGNORE_WORDS = ("accrue, accrued, accrual, accruals, accr, acc, for, of, the, to, and, month, mth, monthly, "
                        "inv, invoice, est, estimate, estimated, reversal, reverse, reversing, reversed, fy, period, "
                        "per, on, in, re, a")


class Behaviour(models.TextChoices):
    """How a cost behaves, which decides how it is accrued."""

    FIXED = "fixed", "Fixed recurring"
    VARIABLE = "variable", "Variable recurring"
    DRIVER = "driver", "Driver-linked"
    PERIODIC = "periodic", "Periodic, non-monthly"
    ONE_OFF = "one_off", "One-off"


class Method(models.TextChoices):
    """How a run works out the amount to accrue."""

    FIXED = "fixed", "Fixed amount"
    RUN_RATE = "run_rate", "Run-rate × seasonality"
    DRIVER_RATE = "driver_rate", "Driver volume × rate"
    PREPAYMENT = "prepayment", "Prepayment / amortisation (not accrued)"
    EVIDENCE_ONLY = "evidence_only", "Only from evidence (a bill after month end)"
    NONE = "none", "Don't accrue"


METHOD_FOR = {Behaviour.FIXED: Method.FIXED, Behaviour.VARIABLE: Method.RUN_RATE,
              Behaviour.DRIVER: Method.DRIVER_RATE, Behaviour.PERIODIC: Method.PREPAYMENT,
              Behaviour.ONE_OFF: Method.EVIDENCE_ONLY}

# Shown with each section in admin: examples and how it is accrued.
BEHAVIOUR_GUIDE = {
    Behaviour.FIXED: ("Software subscriptions, leases, contracted cleaning", "Exact amount"),
    Behaviour.VARIABLE: ("Electricity, water, gas", "Run-rate × seasonality"),
    Behaviour.DRIVER: ("Security per event, laundry per room-night, casual labour per cover",
                       "Driver volume × learned rate"),
    Behaviour.PERIODIC: ("Quarterly rates, annual insurance", "Prepayment or amortisation, not accrual"),
    Behaviour.ONE_OFF: ("Repairs, consultants", "Only accrue from evidence (cut-off scan, PO, user input)"),
}


def _words(text):
    return {w.strip().lower() for w in (text or "").split(",") if w.strip()}


class Accrual(TenantModel):
    """An organisation's accrual set-up: accounts, thresholds, and the result of its last history scan."""

    account_codes = models.CharField(
        "accrual accounts", max_length=255, default="2000",
        help_text="Code(s) of the balance sheet account(s) accruals are posted to, comma-separated, e.g. 2000. "
                  "Runs credit the first one.")
    history_months = models.PositiveSmallIntegerField(
        default=24, help_text="How many months of history to learn from, ending with the last full month.")
    materiality = models.DecimalField(
        "materiality floor", max_digits=14, decimal_places=2, default=500,
        help_text="A run leaves out accruals below this.")
    driver_categories = models.ManyToManyField(
        TrackingCategory, blank=True, related_name="+", verbose_name="driver tracking categories",
        help_text="Tracking categories that count activity, e.g. Event. Their options are drivers, not locations, "
                  "so one profile can cover many events. Blank: any category whose name mentions an event.")

    # Thresholds the scan uses. Change them and scan again.
    fixed_variation = models.DecimalField(
        "fixed if amounts vary less than (%)", max_digits=5, decimal_places=2, default=Decimal("5"),
        help_text="Month-to-month variation (standard deviation ÷ average) below this makes a cost fixed.")
    recurring_coverage = models.DecimalField(
        "recurring if accrued in at least (%) of months", max_digits=5, decimal_places=2, default=Decimal("60"))
    bill_tolerance = models.DecimalField(
        "bill match tolerance (%)", max_digits=5, decimal_places=2, default=Decimal("35"),
        help_text="A bill within this much of the accrual is taken as the bill for it.")
    bill_window_days = models.PositiveSmallIntegerField(
        "bill window (days after month end)", default=75,
        help_text="How long after month end a bill can arrive and still be the bill for that month.")
    dormant_months = models.PositiveSmallIntegerField(
        "not recent after (months)", default=6, help_text="Profiles not accrued for this long are marked not recent.")
    min_driver_options = models.PositiveSmallIntegerField(
        "driver-linked if tagged with at least (options)", default=3)
    trailing_months = models.PositiveSmallIntegerField(
        "run-rate months", default=3, help_text="Run-rate is the average of this many latest months.")
    driver_words = models.TextField(
        default=DEFAULT_DRIVER_WORDS, blank=True,
        help_text="Words in a description that suggest a cost follows activity, comma-separated.")
    ignore_words = models.TextField(
        "words ignored when grouping", default=DEFAULT_IGNORE_WORDS, blank=True,
        help_text="Taken out of descriptions before grouping, with month names, years and numbers.")
    excluded_keys = models.JSONField(
        "deleted profiles", default=list, blank=True,
        help_text="Profiles you deleted; a scan doesn't bring them back. Remove a key to let it.")

    # Journals made by a run.
    journal_tax_rate = models.CharField(
        max_length=50, default="BAS Excluded", help_text="Tax rate name on accrual journal lines in the import file.")
    date_format = models.CharField(max_length=20, default="%d/%m/%Y", help_text="Date format in the import file.")

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

    @property
    def driver_word_set(self):
        return _words(self.driver_words)

    @property
    def ignore_word_set(self):
        return _words(self.ignore_words)


class AccrualProfile(TenantModel):
    """One recurring cost learned from the accrual history: supplier (or description) × expense account ×
    location/department tracking. Driver tracking such as events is not part of the key; it is what the cost is
    driven by.

    The scan fills in every field. A field you change in admin is listed in ``kept_fields`` and a later scan
    leaves it alone; clear the list to let the scan relearn it."""

    accrual = models.ForeignKey(Accrual, on_delete=models.CASCADE, related_name="profiles")
    key = models.CharField(max_length=255, help_text="What the scan groups by; stable across scans.")
    name = models.CharField(max_length=255)
    behaviour = models.CharField(max_length=20, choices=Behaviour.choices)
    detected_behaviour = models.CharField(max_length=20, choices=Behaviour.choices)
    supplier = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    supplier_found_by = models.CharField(max_length=20, blank=True, help_text="description, bills or you")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+",
                                help_text="The cost account a run debits.")
    tracking = models.ManyToManyField(TrackingOption, blank=True, related_name="+")

    # The rule: how a run works out the amount. All editable.
    method = models.CharField(max_length=20, choices=Method.choices, default=Method.EVIDENCE_ONLY)
    amount = models.DecimalField(null=True, blank=True, help_text="Fixed amount, or the run-rate base.", **MONEY)
    driver = models.CharField(max_length=100, blank=True, help_text="e.g. Event. Blank: not driver-linked.")
    driver_category = models.ForeignKey(TrackingCategory, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+", help_text="Where a run counts the driver volume.")
    driver_rate = models.DecimalField(null=True, blank=True, help_text="Amount per driver unit.", **MONEY)
    seasonality = models.JSONField(default=dict, blank=True,
                                   help_text='Run-rate multiplier per month, e.g. {"Jan": 1.3}. Blank: none.')
    reverse_next_month = models.BooleanField(default=True, help_text="Reverse on day 1 of the next month.")
    skip_if_bill_posted = models.BooleanField(
        default=True, help_text="Leave it out of a run when the month's bill is already in the ledger.")
    billing_lag_days = models.FloatField(null=True, blank=True, help_text="Month end → bill date, median.")

    # What the scan measured.
    months_accrued = models.PositiveSmallIntegerField(default=0)
    first_month = models.DateField(null=True, blank=True)
    last_month = models.DateField(null=True, blank=True)
    average = models.DecimalField(default=0, **MONEY)
    last_amount = models.DecimalField(default=0, **MONEY)
    amount_model = models.JSONField(default=dict, blank=True,
                                    help_text="Statistics: median, min, max, variation, monthly amounts.")
    lag_std = models.FloatField(null=True, blank=True)
    accuracy_mape = models.FloatField(null=True, blank=True,
                                      help_text="Average gap between what was accrued and the bill.")
    confidence = models.FloatField(default=0)
    is_current = models.BooleanField(default=True, help_text="Accrued recently (see the set-up's threshold).")
    outcomes = models.JSONField(default=list, blank=True,
                                help_text="Closed Expectation Ledger lines: expected vs actual, per period.")

    memo = models.TextField(blank=True, help_text="What iLA found in the history.")
    rule = models.JSONField("learned rule", default=dict, blank=True,
                            help_text="The rule as the last scan proposed it, for comparison.")
    rule_text = models.TextField("rule", blank=True, editable=False)
    evidence = models.JSONField(default=list, blank=True, help_text="The postings the profile was learned from.")

    is_active = models.BooleanField(default=True, help_text="Untick to stop iLA proposing this accrual.")
    notes = models.TextField(blank=True, help_text="Reviewer notes, e.g. 'bills quarterly in arrears'.")
    kept_fields = models.JSONField(
        "your changes", default=list, blank=True,
        help_text="Fields you changed; a scan leaves them alone. Empty the list to let the scan relearn them.")

    # Fields a scan may set and a person may override.
    LEARNED = ("name", "behaviour", "supplier", "account", "tracking", "method", "amount", "driver",
               "driver_category", "driver_rate", "seasonality", "reverse_next_month", "skip_if_bill_posted",
               "billing_lag_days", "memo")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["accrual", "key"], name="accounting_accrualprofile_key_uniq")]
        ordering = ["-average"]
        verbose_name = "accrual profile"

    def __str__(self):
        return self.name

    @property
    def is_reviewed(self):
        return bool(self.kept_fields) or bool(self.notes) or not self.is_active

    def keep(self, fields):
        """Record fields a person changed, so scans leave them alone."""
        fields = [f for f in fields if f in self.LEARNED]
        if "behaviour" in fields and "method" not in fields and "method" not in self.kept_fields:
            self.method = METHOD_FOR[self.behaviour]
            fields.append("method")
        self.kept_fields = sorted(set(self.kept_fields) | set(fields))
        if "supplier" in fields:
            self.supplier_found_by = "you"

    def save(self, *args, **kwargs):
        from .rules import describe

        self.rule_text = describe(self)
        super().save(*args, **kwargs)


class State(models.TextChoices):
    """An expectation's life: predicted from the rule, evidenced by a bill or a person, posted to the ledger,
    closed when the actual bill is in and the variance is known."""

    PREDICTED = "predicted", "Predicted"
    EVIDENCED = "evidenced", "Evidenced"
    POSTED = "posted", "Posted"
    CLOSED = "closed", "Closed"


STATE_ORDER = [State.PREDICTED, State.EVIDENCED, State.POSTED, State.CLOSED]


class LedgerJournal(TenantModel):
    """One run's journal in the Expectation Ledger, e.g. TECC accruals for September 2026. Its state is the least
    advanced of its lines' states."""

    class Kind(models.TextChoices):
        ACCRUAL = "accrual", "Accrual"

    accrual = models.ForeignKey(Accrual, on_delete=models.PROTECT, related_name="journals")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ACCRUAL)
    period_end = models.DateField(help_text="Last day of the month being closed.")
    narration = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PREDICTED, editable=False)
    skipped = models.JSONField(default=list, blank=True, help_text="Profiles the run left out, and why.")
    notes = models.TextField(blank=True)
    import_file = models.ForeignKey(ImportFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["accrual", "kind", "period_end"], name="accounting_journal_period_uniq")]
        ordering = ["-period_end"]
        verbose_name = "Expectation Ledger journal"
        verbose_name_plural = "Expectation Ledger"

    def __str__(self):
        return self.narration or f"{self.get_kind_display()}s {self.period_end:%b %Y}"

    def refresh_state(self):
        states = [line.state for line in self.lines.all()]
        state = min(states, key=STATE_ORDER.index) if states else State.PREDICTED
        if state != self.state:
            self.state = state
            self.save(update_fields=["state", "updated_at"])
        return state

    @property
    def total_expected(self):
        return sum((line.expected_amount for line in self.lines.all()), Decimal("0"))


class Expectation(TenantModel):
    """One expected entry: what iLA expects to be owed for a cost at period end, with its evidence and the history
    of every state change."""

    journal = models.ForeignKey(LedgerJournal, on_delete=models.CASCADE, related_name="lines")
    profile = models.ForeignKey(AccrualProfile, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="expectations", help_text="Blank for a line you added.")
    description = models.CharField(max_length=255)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    tracking = models.ManyToManyField(TrackingOption, blank=True, related_name="+")
    supplier = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    expected_amount = models.DecimalField(**MONEY)
    basis = models.CharField(max_length=255, blank=True, help_text="How the amount was worked out.")
    reverse_next_month = models.BooleanField(default=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PREDICTED)
    confidence = models.FloatField(default=0)
    evidence = models.JSONField(default=list, blank=True, help_text="Bills, files and notes supporting it.")
    history = models.JSONField(default=list, blank=True, help_text="Every change: when, who or what, and why.")
    actual_amount = models.DecimalField(null=True, blank=True, **MONEY)
    variance = models.DecimalField(null=True, blank=True, help_text="Actual − expected.", **MONEY)
    is_manual = models.BooleanField(default=False, help_text="Added or changed by a person; a re-run keeps it.")

    class Meta(TenantModel.Meta):
        ordering = ["pk"]
        verbose_name = "expectation"

    def __str__(self):
        return f"{self.description} {self.expected_amount}"

    def log(self, by, why, **changes):
        """Append a history entry. ``by`` is a user or the name of what changed it (e.g. "run", "bill check")."""
        who = getattr(by, "get_username", None)
        self.history = self.history + [{"at": timezone.now().isoformat(timespec="seconds"),
                                        "by": who() if who else str(by), "why": why,
                                        **{k: str(v) for k, v in changes.items()}}]

    def move(self, state, by, why, **changes):
        if state != self.state:
            changes = {"from": self.state, "to": state, **changes}
            self.state = state
        self.log(by, why, **changes)


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


def _remember_deleted(sender, instance, **kwargs):
    """A profile a person deletes stays deleted: the scan skips its key (listed on the set-up, where it can be
    removed again). Scans never delete profiles themselves."""
    accrual = Accrual.objects.filter(pk=instance.accrual_id).first()
    if accrual is not None and instance.key not in accrual.excluded_keys:
        accrual.excluded_keys = accrual.excluded_keys + [instance.key]
        accrual.save(update_fields=["excluded_keys", "updated_at"])


models.signals.post_delete.connect(_remember_deleted, sender=AccrualProfile)
