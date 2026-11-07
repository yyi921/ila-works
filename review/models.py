"""The month-end P&L review (/review): the core checks every business gets, the review requests an organisation adds
in its own words, and the findings people work through. See docs/Month-end Review — Design.md.

Settings, core checks and review requests are edited in admin. A finding's status and note are kept by its key
(check + subject) for the month, so re-running /review never loses what a person said; notes are shown again when
the same subject comes up in a later month.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TenantModel

MONEY = {"max_digits": 19, "decimal_places": 2}


class PackView(models.TextChoices):
    LANDING = "landing", "Landing (Xero now + To post + Expected)"
    TO_POST = "to_post", "Xero now + To post"
    XERO = "xero", "Xero now only"


class ReviewSettings(TenantModel):
    """One per organisation: which accounts make up the ratios, the trial-run length and the pack's view. Account
    lists are codes and ranges, e.g. "6300-6380, 6390". Blank means iLA works them out (see review.checks)."""

    cogs_accounts = models.CharField("COGS accounts", max_length=255, blank=True,
                                     help_text="Blank: every Direct Costs account (Cost of sales).")
    labour_accounts = models.CharField(max_length=255, blank=True,
                                       help_text="Blank: accounts whose names say wages, salaries, super, payroll, "
                                                 "leave, Workcover or staff. TECC: 6300-6380.")
    interest_accounts = models.CharField(max_length=255, blank=True,
                                         help_text="Interest expense, added back for EBITDA. Blank: cost accounts "
                                                   "named interest.")
    tax_accounts = models.CharField("income tax accounts", max_length=255, blank=True,
                                    help_text="Added back for EBITDA. Blank: cost accounts named income tax.")
    depreciation_accounts = models.CharField("depreciation and amortisation accounts", max_length=255, blank=True,
                                             help_text="Added back for EBITDA. Blank: Depreciation accounts and cost "
                                                       "accounts named depreciation or amortisation.")
    trial_months = models.PositiveSmallIntegerField(default=2, help_text="Months a review request's trial run "
                                                                         "covers.")
    pack_view = models.CharField(max_length=20, choices=PackView.choices, default=PackView.LANDING,
                                 help_text="What the management pack shows.")

    class Meta(TenantModel.Meta):
        verbose_name = verbose_name_plural = "review settings"
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation"], name="review_settings_one_per_org")]

    def __str__(self):
        return f"Review settings: {self.organisation}"

    @classmethod
    def for_organisation(cls, organisation):
        """The organisation's settings and its eight core checks, created with the defaults the first time."""
        obj, _ = cls.objects.get_or_create(organisation=organisation)
        have = set(obj.checks.values_list("code", flat=True))
        CoreCheck.objects.bulk_create([CoreCheck(organisation=organisation, settings=obj, code=code)
                                       for code in CHECKS if code not in have])
        return obj


# code -> (name, kind, compared with). Kinds: "lines" (every P&L line and subtotal), "ratio".
CHECKS = {
    "C1": ("Month vs last month", "lines", ("last_month",)),
    "C2": ("Month vs same month last year", "lines", ("last_year",)),
    "C3": ("Month vs budget", "lines", ("budget",)),
    "C4": ("Year to date vs budget", "lines", ("ytd_budget",)),
    "C5": ("COGS % of revenue", "ratio", ("last_month", "last_year", "budget")),
    "C6": ("Labour % of revenue", "ratio", ("last_month", "last_year", "budget")),
    "C7": ("EBITDA % of revenue", "ratio", ("last_month", "last_year", "budget")),
    "C8": ("Net profit % of revenue", "ratio", ("last_month", "last_year", "budget")),
}


class Join(models.TextChoices):
    ANY = "any", "Any one of them (or)"
    ALL = "all", "All of them (and)"


class CoreCheck(TenantModel):
    """One of the eight core P&L checks, with its switch and materiality. A movement is raised when it passes the
    thresholds that are set: any one of them, or all of them. With none set, every movement is raised. For the
    ratio checks the amount is the movement's effect in dollars (points moved × this month's revenue)."""

    settings = models.ForeignKey(ReviewSettings, on_delete=models.CASCADE, related_name="checks")
    code = models.CharField(max_length=3, choices=[(c, f"{c} {v[0]}") for c, v in CHECKS.items()])
    enabled = models.BooleanField(default=True)
    amount = models.DecimalField("over $", null=True, blank=True, default=Decimal("1000"),
                                 help_text="Raise movements over this amount, either way.", **MONEY)
    percent = models.DecimalField("over %", max_digits=7, decimal_places=2, null=True, blank=True,
                                  help_text="Raise movements over this % of what it's compared with.")
    points = models.DecimalField("over points", max_digits=7, decimal_places=2, null=True, blank=True,
                                 help_text="Ratio checks: raise moves of more than this many percentage points.")
    join = models.CharField("raise when", max_length=3, choices=Join.choices, default=Join.ANY)

    class Meta(TenantModel.Meta):
        ordering = ["code"]
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["settings", "code"], name="review_corecheck_code_uniq")]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def name(self):
        return CHECKS[self.code][0]

    @property
    def kind(self):
        return CHECKS[self.code][1]

    def material(self, amount, percent=None, points=None):
        """Whether a movement passes. ``amount`` is in dollars, ``percent`` and ``points`` are in % (10 = 10%);
        a percent of None means there was nothing to compare with, which passes any % threshold."""
        tests = []
        if self.amount is not None:
            tests.append(abs(amount) > self.amount)
        if self.percent is not None:
            tests.append(percent is None or abs(percent) > self.percent)
        if self.points is not None and points is not None:
            tests.append(abs(points) > self.points)
        if not tests:
            return amount != 0
        return all(tests) if self.join == Join.ALL else any(tests)

    def materiality_text(self):
        parts = []
        if self.amount is not None:
            parts.append(f"${self.amount:,.0f}")
        if self.percent is not None:
            parts.append(f"{self.percent:g}%")
        if self.points is not None:
            parts.append(f"{self.points:g} points")
        if not parts:
            return "any movement"
        return "over " + (" and " if self.join == Join.ALL else " or ").join(parts)


class RequestStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    TRIAL = "trial", "Trial run"
    CONFIRMED = "confirmed", "Confirmed"


class ReviewRequest(TenantModel):
    """A review request (RR-01…): what a person wants iLA to look at, in their own words. A trial run turns the prompt
    into a saved check (``spec``) and shows its result; an admin confirms it, and from then on /review runs the
    confirmed check with code, never re-reading the prompt. Editing the prompt leaves the confirmed check running
    until the new one is confirmed."""

    number = models.PositiveIntegerField(editable=False)
    title = models.CharField(max_length=120, blank=True, help_text="Short name shown in /review. Blank: iLA names it.")
    prompt = models.TextField("request", help_text='In your own words, e.g. "iLA, please look at any revenue '
                                                   'unassigned to any events."')
    trial_months = models.PositiveSmallIntegerField(null=True, blank=True,
                                                    help_text="Blank: the organisation's setting (2).")
    enabled = models.BooleanField("on", default=True, help_text="Off: /review skips it.")
    status = models.CharField(max_length=20, choices=RequestStatus.choices, default=RequestStatus.DRAFT,
                              editable=False)
    job_status = models.CharField(max_length=20, blank=True, editable=False)
    job_message = models.CharField(max_length=500, blank=True, editable=False)
    job_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    # The latest trial run
    spec = models.JSONField(default=dict, blank=True, editable=False, help_text="The check iLA worked out.")
    plain = models.TextField("check in plain English", blank=True, editable=False)
    cannot = models.TextField("what iLA can't do", blank=True, editable=False)
    trial_result = models.JSONField(default=dict, blank=True, editable=False)
    trial_prompt = models.TextField(blank=True, editable=False)
    trial_at = models.DateTimeField(null=True, blank=True, editable=False)
    trial_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+", editable=False)
    # The confirmed check /review runs
    confirmed_spec = models.JSONField(default=dict, blank=True, editable=False)
    confirmed_plain = models.TextField(blank=True, editable=False)
    confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+", editable=False)
    history = models.JSONField(default=list, blank=True, editable=False,
                               help_text="Every trial run and confirmation: when, who, the prompt and the check.")

    class Meta(TenantModel.Meta):
        ordering = ["number"]
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "number"], name="review_request_number_uniq")]
        permissions = [("confirm_reviewrequest", "Can confirm review requests")]

    def __str__(self):
        return f"{self.code} {self.title or self.prompt[:60]}"

    @property
    def code(self):
        return f"RR-{self.number:02d}"

    def save(self, *args, **kwargs):
        if not self.number:
            last = ReviewRequest.objects.filter(organisation=self.organisation).aggregate(models.Max("number"))
            self.number = (last["number__max"] or 0) + 1
        super().save(*args, **kwargs)

    @property
    def runs(self):
        """Whether /review runs it: on and confirmed (an edited prompt waiting for its trial still runs the old
        check)."""
        return bool(self.confirmed_spec) and self.enabled

    def log(self, user, what, **details):
        self.history = self.history + [{"at": timezone.now().isoformat(timespec="seconds"),
                                        "by": getattr(user, "username", "") or "", "what": what, **details}]


class FindingStatus(models.TextChoices):
    OPEN = "open", "Open"
    LATER = "later", "Later"
    OK = "ok", "OK"
    NOTED = "noted", "Noted"
    ACTED = "acted", "Acted on"


REVIEWED = (FindingStatus.OK, FindingStatus.NOTED, FindingStatus.ACTED)


class ReviewFinding(TenantModel):
    """What a person decided about one finding in a month: its status and note. ``key`` is the check and subject
    ("C3:6345", "C6:labour", "RR-01:INV-2231"); ``subject`` is the check-free part, so a note on 6345 in one month is
    shown again when 6345 comes up in a later month."""

    period_end = models.DateField()
    key = models.CharField(max_length=200)
    subject = models.CharField(max_length=200, db_index=True)
    label = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=FindingStatus.choices, default=FindingStatus.OPEN)
    note = models.TextField(blank=True)
    action = models.CharField(max_length=200, blank=True, help_text="The command used to act on it.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta(TenantModel.Meta):
        ordering = ["-period_end", "key"]
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "period_end", "key"], name="review_finding_key_uniq")]

    def __str__(self):
        return f"{self.period_end:%b %Y} {self.key}: {self.get_status_display()}"
