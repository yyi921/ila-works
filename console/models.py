from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from core.models import TenantModel


class CustomCommand(TenantModel):
    """A command an organisation's builders made, run as ``/_name``. Native iLA commands never start with "_", so
    the two can't clash. ``definition`` holds what the template needs: for a shortcut, the native command steps."""

    class Template(models.TextChoices):
        SHORTCUT = "shortcut", "Start from scratch"
        CREATE_INVOICE = "createinvoice", "createInvoice"

    name = models.CharField(max_length=40, help_text='Starts with "_", e.g. _weekly.')
    template = models.CharField(max_length=20, choices=Template.choices, default=Template.SHORTCUT)
    summary = models.CharField(max_length=200)
    definition = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "name"], name="console_customcommand_name_uniq"),
        ]
        ordering = ["name"]
        permissions = [("build_commands", "Can build custom commands in the builder")]

    def __str__(self):
        return self.name

    @property
    def steps(self):
        return list(self.definition.get("steps", []))


class InvoiceDraft(TenantModel):
    """A /_createInvoice conversation and the invoice it is building, saved after every answer so it can be
    resumed. ``data`` holds the header (customer, event, number, dates, reference), the lines, what was read from
    each source file (never the file itself), open questions and the numbered choices last offered."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        CANCELLED = "cancelled", "Cancelled"

    class Step(models.TextChoices):
        SETUP = "setup", "Choose the invoice set-up"
        CUSTOMER = "customer", "Customer"
        EVENT = "event", "Event or job"
        SOURCES = "sources", "Billing information"
        REVIEW = "review", "Review"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    command = models.ForeignKey(CustomCommand, null=True, on_delete=models.SET_NULL, related_name="drafts")
    setup = models.ForeignKey("core.InvoiceSetup", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    step = models.CharField(max_length=20, choices=Step.choices, default=Step.SETUP)
    data = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True)
    import_file = models.ForeignKey("core.ImportFile", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    class Meta(TenantModel.Meta):
        ordering = ["-updated_at"]

    def __str__(self):
        customer = (self.data.get("customer") or {}).get("name", "")
        return f"Invoice draft {self.pk} {customer} ({self.get_status_display()})"


class TagDraft(TenantModel):
    """A /createTag conversation: which dimension it creates or updates, and the values read from the user's CSV,
    saved after every answer so it can be resumed. ``data`` holds the target (a Xero tracking category to replace,
    an iLA dimension to update, or a new one), the name, the rows and fields read from the file, problems and
    warnings, and the numbered choices last offered. Nothing is saved to the dimension until it is approved."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        CANCELLED = "cancelled", "Cancelled"

    class Step(models.TextChoices):
        TARGET = "target", "New or replace"
        NAME = "name", "Name"
        FILE = "file", "Values file"
        REVIEW = "review", "Review"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    step = models.CharField(max_length=20, choices=Step.choices, default=Step.TARGET)
    data = models.JSONField(default=dict, blank=True)
    dimension = models.ForeignKey("core.Dimension", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")

    class Meta(TenantModel.Meta):
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Tag draft {self.pk} {self.data.get('name', '')} ({self.get_status_display()})"


class TagRun(TenantModel):
    """A /tag preview waiting for approval: the transactions found, the value proposed for each with its confidence
    and reason, and what the user changed (skipped rows, values set by hand). ``data`` holds the command, period,
    copy summary and rows. Nothing is tagged until it is approved."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    dimension = models.ForeignKey("core.Dimension", on_delete=models.CASCADE, related_name="+")
    data = models.JSONField(default=dict, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Tag run {self.pk} {self.dimension} ({self.get_status_display()})"


class SplitRule(TenantModel):
    """A named /splittag rule, saved when a CSV is approved and re-run with ``/splittag --<name> --<period>``.
    ``scope``: {"accounts": "6300-6380; 6400", "types": ["journals"], "contact": "", "desc": ""}.
    ``allocations``: [{"value": DimensionValue id, "code", "amount": "100" or null, "percent": "30" or null}];
    fixed amounts are taken first, percentages apply to what's left, anything left over stays untagged."""

    name = models.CharField(max_length=40)
    dimension = models.ForeignKey("core.Dimension", on_delete=models.CASCADE, related_name="+")
    scope = models.JSONField(default=dict)
    allocations = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    last_run = models.CharField(max_length=100, blank=True, help_text="The period it was last applied to.")

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint("organisation", Lower("name"), name="console_splitrule_name_uniq",
                                    violation_error_message="There's already a split rule with this name."),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name


class SplitRun(TenantModel):
    """A /splittag preview waiting for approval: the rules, the period, the lines in each rule's scope and the
    shares worked out for them. Nothing is tagged (or saved as a rule) until it is approved."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    data = models.JSONField(default=dict, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Split run {self.pk} ({self.get_status_display()})"


class CommandReference(models.Model):
    """One native iLA command, as defined in code: syntax, parameters and examples. Refreshed from the command
    registry after every migrate and by ``manage.py sync_command_reference``, so it never drifts from what the
    console actually accepts. ``instructions`` is the only field edited by hand: guidance on when and how to use it."""

    name = models.CharField(max_length=40, unique=True)
    aliases = models.CharField(max_length=200, blank=True)
    summary = models.CharField(max_length=300)
    usage = models.TextField()
    parameters = models.JSONField(default=list, blank=True, help_text="[[syntax, meaning], ...]")
    examples = models.JSONField(default=list, blank=True)
    takes_input_file = models.BooleanField(default=False)
    needs_connection = models.BooleanField(default=True, help_text="Reads from the connected accounting system.")
    instructions = models.TextField(blank=True, help_text="How and when to use it, in your own words. Kept when "
                                                          "the rest is refreshed from code.")
    sort_order = models.PositiveIntegerField(default=0)
    in_code = models.BooleanField(default=True, help_text="Off when the command has been removed from iLA.")
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order"]
        verbose_name = "native command"

    def __str__(self):
        return f"/{self.name}"
