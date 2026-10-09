"""Canonical data model.

Source-agnostic: nothing here knows about Xero. Connector apps (e.g. ``xero``) map their source schema onto
these models. Every tenant row carries ``organisation`` plus ``source_system``/``source_id`` for tie-back to the
record it came from. Rows created in iLA itself use ``source_system="ila"`` and may leave ``source_id`` blank.

Sign convention: amounts are stored as the source presents them on the document (positive totals). Journal lines
are signed, debit positive.
"""

import hashlib
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

MONEY = {"max_digits": 19, "decimal_places": 2}
QUANTITY = {"max_digits": 19, "decimal_places": 4}


class SourceSystem(models.TextChoices):
    XERO = "xero", "Xero"
    ILA = "ila", "iLA"
    IMPORT = "import", "Customer file import"


class LineAmountType(models.TextChoices):
    EXCLUSIVE = "exclusive", "Tax exclusive"
    INCLUSIVE = "inclusive", "Tax inclusive"
    NO_TAX = "no_tax", "No tax"


class Organisation(models.Model):
    """A customer organisation: the tenant. One organisation may later be connected to several source systems,
    so the per-system identifiers (e.g. Xero tenant ID) live on the connector's connection model."""

    name = models.CharField(max_length=255)
    base_currency = models.CharField(max_length=3, default="AUD")
    timezone = models.CharField(max_length=64, default="Australia/Brisbane")
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="organisations")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    def for_organisation(self, organisation):
        return self.filter(organisation=organisation)


class TenantModel(models.Model):
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="+")
    source_system = models.CharField(max_length=20, choices=SourceSystem.choices, default=SourceSystem.ILA)
    source_id = models.CharField(max_length=255, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    # Hash of the source payload as last synced; a change marks tags on this row stale.
    source_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=["organisation", "source_system", "source_id"],
                condition=~Q(source_id=""),
                name="%(app_label)s_%(class)s_source_uniq",
            )
        ]


# --- Source mirrors -------------------------------------------------------------------------------------------


class Account(TenantModel):
    class AccountClass(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"

    code = models.CharField(max_length=20, blank=True)
    name = models.CharField(max_length=255)
    account_class = models.CharField(max_length=20, choices=AccountClass.choices)
    source_type = models.CharField(max_length=50, blank=True, help_text="Account type as named in the source.")
    is_bank_account = models.BooleanField(default=False)
    tax_code = models.CharField(max_length=50, blank=True)
    currency_code = models.CharField(max_length=3, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["organisation", "code"])]

    def __str__(self):
        return f"{self.code} {self.name}".strip()


class Contact(TenantModel):
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    tax_number = models.CharField(max_length=50, blank=True)
    is_customer = models.BooleanField(default=False)
    is_supplier = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["organisation", "name"])]

    def __str__(self):
        return self.name


class TrackingCategory(TenantModel):
    """Mirrors the source system's own tracking categories (read-only in v1)."""

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        verbose_name_plural = "tracking categories"

    def __str__(self):
        return self.name


class TrackingOption(TenantModel):
    category = models.ForeignKey(TrackingCategory, on_delete=models.CASCADE, related_name="options")
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.category}: {self.name}"


class Document(TenantModel):
    """Invoices, bills and credit notes."""

    class Type(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        BILL = "bill", "Bill"
        CREDIT_NOTE = "credit_note", "Credit note"

    class Ledger(models.TextChoices):
        RECEIVABLE = "receivable", "Accounts receivable"
        PAYABLE = "payable", "Accounts payable"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        AUTHORISED = "authorised", "Authorised"
        PAID = "paid", "Paid"
        VOIDED = "voided", "Voided"
        DELETED = "deleted", "Deleted"

    type = models.CharField(max_length=20, choices=Type.choices)
    # Needed to tell sales credit notes from supplier credit notes.
    ledger = models.CharField(max_length=20, choices=Ledger.choices)
    number = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=255, blank=True)
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="documents")
    status = models.CharField(max_length=20, choices=Status.choices)
    date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    currency_code = models.CharField(max_length=3, blank=True)
    currency_rate = models.DecimalField(max_digits=19, decimal_places=6, null=True, blank=True)
    line_amount_type = models.CharField(max_length=20, choices=LineAmountType.choices, blank=True)
    subtotal = models.DecimalField(default=0, **MONEY)
    total_tax = models.DecimalField(default=0, **MONEY)
    total = models.DecimalField(default=0, **MONEY)
    amount_due = models.DecimalField(default=0, **MONEY)
    amount_paid = models.DecimalField(default=0, **MONEY)
    amount_credited = models.DecimalField(default=0, **MONEY)

    class Meta(TenantModel.Meta):
        indexes = [
            models.Index(fields=["organisation", "ledger", "status"]),
            models.Index(fields=["organisation", "date"]),
            models.Index(fields=["organisation", "number"]),
        ]

    def __str__(self):
        return self.number or f"{self.get_type_display()} {self.source_id}"


class SourceLine(TenantModel):
    line_number = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)
    item_code = models.CharField(max_length=100, blank=True)
    quantity = models.DecimalField(null=True, blank=True, **QUANTITY)
    unit_amount = models.DecimalField(null=True, blank=True, **QUANTITY)
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    tax_code = models.CharField(max_length=50, blank=True)
    tax_amount = models.DecimalField(default=0, **MONEY)
    line_amount = models.DecimalField(default=0, **MONEY)
    tracking = models.ManyToManyField(TrackingOption, blank=True, related_name="+")

    class Meta(TenantModel.Meta):
        abstract = True
        ordering = ["line_number", "pk"]

    def __str__(self):
        return self.description[:60] or f"Line {self.line_number}"


class DocumentLine(SourceLine):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="lines")


class BankTransaction(TenantModel):
    class Direction(models.TextChoices):
        RECEIVE = "receive", "Receive money"
        SPEND = "spend", "Spend money"

    class Status(models.TextChoices):
        AUTHORISED = "authorised", "Authorised"
        VOIDED = "voided", "Voided"
        DELETED = "deleted", "Deleted"

    direction = models.CharField(max_length=10, choices=Direction.choices)
    source_type = models.CharField(max_length=50, blank=True, help_text="Transaction type as named in the source.")
    bank_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    date = models.DateField()
    reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    is_reconciled = models.BooleanField(default=False)
    currency_code = models.CharField(max_length=3, blank=True)
    currency_rate = models.DecimalField(max_digits=19, decimal_places=6, null=True, blank=True)
    line_amount_type = models.CharField(max_length=20, choices=LineAmountType.choices, blank=True)
    subtotal = models.DecimalField(default=0, **MONEY)
    total_tax = models.DecimalField(default=0, **MONEY)
    total = models.DecimalField(default=0, **MONEY)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["organisation", "date"])]

    def __str__(self):
        return f"{self.get_direction_display()} {self.total} on {self.date}"


class BankTransactionLine(SourceLine):
    """Split lines of a bank transaction: they carry the P&L account, so tagging and the tie-out need them."""

    bank_transaction = models.ForeignKey(BankTransaction, on_delete=models.CASCADE, related_name="lines")


class Journal(TenantModel):
    """Manual journals from the source system (v1 does not use Xero's premium Journals endpoint)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        VOIDED = "voided", "Voided"
        DELETED = "deleted", "Deleted"

    number = models.CharField(max_length=255, blank=True)
    date = models.DateField()
    narration = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    line_amount_type = models.CharField(max_length=20, choices=LineAmountType.choices, blank=True)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["organisation", "date"])]

    def __str__(self):
        return self.number or f"Journal {self.date}"


class JournalLine(TenantModel):
    journal = models.ForeignKey(Journal, on_delete=models.CASCADE, related_name="lines")
    line_number = models.PositiveIntegerField(default=0)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    description = models.TextField(blank=True)
    amount = models.DecimalField(help_text="Signed: debit positive, credit negative.", **MONEY)
    tax_code = models.CharField(max_length=50, blank=True)
    tax_amount = models.DecimalField(default=0, **MONEY)
    tracking = models.ManyToManyField(TrackingOption, blank=True, related_name="+")

    class Meta(TenantModel.Meta):
        ordering = ["line_number", "pk"]

    def __str__(self):
        return f"{self.account} {self.amount}"


# --- iLA analysis layer (never written back to the source) ----------------------------------------------------


class Dimension(TenantModel):
    """A customer-defined analysis dimension, e.g. event, department, venue, campaign."""

    class AttributeType(models.TextChoices):
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        DATE = "date", "Date (YYYY-MM-DD)"
        BOOLEAN = "boolean", "Yes/no"

    code = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    tracking_category = models.ForeignKey(
        TrackingCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Source tracking category this dimension was imported from, if any.",
    )
    attribute_fields = models.JSONField(
        default=list, blank=True,
        help_text='Customer-defined fields for this dimension\'s values: a list of {"key", "label", "type", '
                  '"required"} with type one of text, number, date, boolean.',
    )
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["organisation", "code"], name="core_dimension_code_uniq")
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if not isinstance(self.attribute_fields, list):
            raise ValidationError({"attribute_fields": "Must be a list of field definitions."})
        keys = set()
        for spec in self.attribute_fields:
            key = spec.get("key") if isinstance(spec, dict) else None
            if not key or not isinstance(key, str):
                raise ValidationError({"attribute_fields": f"Field definition {spec!r} needs a text key."})
            if key in keys:
                raise ValidationError({"attribute_fields": f"Field key {key!r} is defined twice."})
            if spec.get("type", "text") not in self.AttributeType.values:
                raise ValidationError({"attribute_fields": f"Field {key!r} has unknown type {spec.get('type')!r}."})
            keys.add(key)


def _attribute_ok(kind, value):
    if kind == Dimension.AttributeType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == Dimension.AttributeType.BOOLEAN:
        return isinstance(value, bool)
    if kind == Dimension.AttributeType.DATE:
        try:
            date.fromisoformat(value)
            return len(value) == 10
        except (TypeError, ValueError):
            return False
    return isinstance(value, str)


class DimensionValue(TenantModel):
    dimension = models.ForeignKey(Dimension, on_delete=models.CASCADE, related_name="values")
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    tracking_option = models.ForeignKey(
        TrackingOption, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    attributes = models.JSONField(default=dict, blank=True,
                                  help_text="Values for the fields defined in the dimension's attribute_fields.")
    is_active = models.BooleanField(default=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["dimension", "code"], name="core_dimensionvalue_code_uniq")
        ]

    def __str__(self):
        return f"{self.dimension}: {self.name}"

    def clean(self):
        """Check ``attributes`` against the dimension's field definitions: no unknown keys, required fields
        present, and each value of the declared type."""
        super().clean()
        if not isinstance(self.attributes, dict):
            raise ValidationError({"attributes": "Must be an object of field values."})
        specs = {spec["key"]: spec for spec in self.dimension.attribute_fields}
        errors = [f"Unknown field {key!r}." for key in self.attributes if key not in specs]
        for key, spec in specs.items():
            value = self.attributes.get(key)
            if value in (None, ""):
                if spec.get("required"):
                    errors.append(f"{spec.get('label', key)} is required.")
            elif not _attribute_ok(spec.get("type", "text"), value):
                errors.append(f"{spec.get('label', key)} must be a {spec.get('type', 'text')} value.")
        if errors:
            raise ValidationError({"attributes": errors})


class Event(TenantModel):
    """Reference data for the event dimension, typically from a customer calendar CSV
    (``source_system="import"``, ``source_id`` = the customer's event ID)."""

    dimension_value = models.OneToOneField(
        DimensionValue, null=True, blank=True, on_delete=models.SET_NULL, related_name="event"
    )
    name = models.CharField(max_length=255)
    venue = models.CharField(max_length=255, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    bump_in_date = models.DateField(null=True, blank=True)
    bump_out_date = models.DateField(null=True, blank=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.CheckConstraint(condition=Q(end_date__gte=F("start_date")), name="core_event_end_after_start"),
            models.CheckConstraint(
                condition=Q(bump_in_date__isnull=True) | Q(bump_in_date__lte=F("start_date")),
                name="core_event_bump_in_before_start",
            ),
            models.CheckConstraint(
                condition=Q(bump_out_date__isnull=True) | Q(bump_out_date__gte=F("end_date")),
                name="core_event_bump_out_after_end",
            ),
        ]
        indexes = [models.Index(fields=["organisation", "start_date"])]

    def __str__(self):
        return self.name


_ONE_LINE = (
    Q(document_line__isnull=False, bank_transaction_line__isnull=True, journal_line__isnull=True)
    | Q(document_line__isnull=True, bank_transaction_line__isnull=False, journal_line__isnull=True)
    | Q(document_line__isnull=True, bank_transaction_line__isnull=True, journal_line__isnull=False)
)


class Tag(TenantModel):
    """Attributes a share (``allocation``) of one source line to one value of a dimension.

    Tags are the per-line result reports read. They are derived from ``Assignment`` rows (what someone or some
    rule decided, at line or transaction level) by ``core.tagging.apply_assignments``.

    A line's live tags within one dimension must sum to exactly 1.0, so P&L by dimension (amount x allocation)
    ties to the source without double counting. That cross-row rule can't be a database constraint: write tags
    only through ``core.tagging`` and check with ``core.tagging.unbalanced_lines``.
    """

    class Method(models.TextChoices):
        RULE = "rule", "Attribution rule"
        TRACKING = "tracking", "From source tracking category"
        IMPORT = "import", "File import"
        MANUAL = "manual", "Manual"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded by a later tagging"
        STALE = "stale", "Stale (source changed)"

    NOT_LIVE = (Status.REJECTED, Status.SUPERSEDED)

    document_line = models.ForeignKey(DocumentLine, null=True, blank=True, on_delete=models.CASCADE, related_name="tags")
    bank_transaction_line = models.ForeignKey(
        BankTransactionLine, null=True, blank=True, on_delete=models.CASCADE, related_name="tags"
    )
    journal_line = models.ForeignKey(JournalLine, null=True, blank=True, on_delete=models.CASCADE, related_name="tags")
    dimension = models.ForeignKey(Dimension, on_delete=models.CASCADE, related_name="+", editable=False)
    dimension_value = models.ForeignKey(DimensionValue, on_delete=models.PROTECT, related_name="tags")
    allocation = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("1"),
                                     help_text="Share of the line attributed to this value, 0 < allocation <= 1.")
    method = models.CharField(max_length=20, choices=Method.choices)
    rule = models.CharField(max_length=100, blank=True, help_text="Identifier of the rule that proposed the tag.")
    confidence = models.CharField(max_length=10, choices=Confidence.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Empty when set by the system.",
    )
    set_at = models.DateTimeField(default=timezone.now)
    line_source_hash = models.CharField(max_length=64, blank=True, help_text="Line's source_hash when tagged.")
    note = models.TextField(blank=True)
    assignment = models.ForeignKey(
        "Assignment", null=True, blank=True, on_delete=models.CASCADE, related_name="tags",
        help_text="The assignment this tag was derived from.",
    )

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.CheckConstraint(condition=_ONE_LINE, name="core_tag_exactly_one_line"),
            models.CheckConstraint(condition=Q(allocation__gt=0, allocation__lte=1), name="core_tag_allocation_range"),
        ] + [
            # A value appears at most once among a line's live tags; splits use different values.
            models.UniqueConstraint(
                fields=[field, "dimension_value"],
                condition=~Q(status__in=["rejected", "superseded"]) & Q(**{f"{field}__isnull": False}),
                name=f"core_tag_live_value_per_{field}",
            )
            for field in ("document_line", "bank_transaction_line", "journal_line")
        ]
        indexes = [models.Index(fields=["organisation", "status"])]

    @property
    def line(self):
        return self.document_line or self.bank_transaction_line or self.journal_line

    @property
    def is_stale(self):
        return self.status == self.Status.STALE or self.line_source_hash != self.line.source_hash

    def save(self, *args, **kwargs):
        self.dimension_id = self.dimension_value.dimension_id
        if not self.pk and not self.line_source_hash:
            self.line_source_hash = self.line.source_hash
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.line} -> {self.dimension_value}"


TARGET_FIELDS = (
    "document_line", "bank_transaction_line", "journal_line",  # line level
    "document", "bank_transaction", "journal",  # transaction level
)
_ONE_TARGET = Q()
for _field in TARGET_FIELDS:
    _ONE_TARGET |= Q(**{f"{_field}__isnull": False}) & Q(
        *(Q(**{f"{other}__isnull": True}) for other in TARGET_FIELDS if other != _field)
    )


class Assignment(TenantModel):
    """A decision that one line, or every line of one transaction, belongs to one or more values of a dimension
    (split by ``AssignmentShare.allocation``). Tagging a group of transactions creates one transaction-level
    assignment per transaction, sharing a ``batch`` id.

    For each line, the winning live assignment is chosen by precedence: manual beats system (rule, tracking,
    import), then line level beats transaction level, then the newest wins. ``core.tagging.apply_assignments``
    turns winners into ``Tag`` rows. A transaction-level assignment also covers lines added later in the source.
    """

    class Level(models.TextChoices):
        LINE = "line", "Line"
        TRANSACTION = "transaction", "Transaction"

    document_line = models.ForeignKey(DocumentLine, null=True, blank=True, on_delete=models.CASCADE,
                                      related_name="assignments")
    bank_transaction_line = models.ForeignKey(BankTransactionLine, null=True, blank=True, on_delete=models.CASCADE,
                                              related_name="assignments")
    journal_line = models.ForeignKey(JournalLine, null=True, blank=True, on_delete=models.CASCADE,
                                     related_name="assignments")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="assignments")
    bank_transaction = models.ForeignKey(BankTransaction, null=True, blank=True, on_delete=models.CASCADE,
                                         related_name="assignments")
    journal = models.ForeignKey(Journal, null=True, blank=True, on_delete=models.CASCADE, related_name="assignments")
    level = models.CharField(max_length=20, choices=Level.choices, editable=False)
    dimension = models.ForeignKey(Dimension, on_delete=models.CASCADE, related_name="assignments")
    method = models.CharField(max_length=20, choices=Tag.Method.choices)
    rule = models.CharField(max_length=100, blank=True, help_text="Identifier of the rule that proposed it.")
    confidence = models.CharField(max_length=10, choices=Tag.Confidence.choices)
    status = models.CharField(max_length=20, choices=Tag.Status.choices, default=Tag.Status.PROPOSED)
    batch = models.UUIDField(null=True, blank=True, db_index=True,
                             help_text="Shared by assignments made together, e.g. tagging a group of transactions.")
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Empty when set by the system.",
    )
    set_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.CheckConstraint(condition=_ONE_TARGET, name="core_assignment_exactly_one_target"),
        ]
        indexes = [models.Index(fields=["organisation", "dimension", "status"])]

    @property
    def target_field(self):
        return next(field for field in TARGET_FIELDS if getattr(self, f"{field}_id"))

    @property
    def target(self):
        return getattr(self, self.target_field)

    @property
    def is_manual(self):
        return self.method == Tag.Method.MANUAL

    def save(self, *args, **kwargs):
        self.level = self.Level.LINE if self.target_field.endswith("_line") else self.Level.TRANSACTION
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.target} -> {self.dimension} ({self.get_method_display()})"


class AssignmentShare(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="shares")
    dimension_value = models.ForeignKey(DimensionValue, on_delete=models.PROTECT, related_name="+")
    allocation = models.DecimalField(max_digits=7, decimal_places=6, default=Decimal("1"))

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["assignment", "dimension_value"], name="core_assignmentshare_uniq"),
            models.CheckConstraint(condition=Q(allocation__gt=0, allocation__lte=1),
                                   name="core_assignmentshare_allocation_range"),
        ]

    def __str__(self):
        return f"{self.dimension_value} {self.allocation}"


class ImportFile(TenantModel):
    """Register of import files built for posting into a target system. Stamping records who approved the file
    and the SHA-256 of its exact bytes; any later edit to the file invalidates the stamp."""

    class Status(models.TextChoices):
        BUILT = "built", "Built"
        STAMPED = "stamped", "Stamped"
        POSTED = "posted", "Posted"
        REJECTED = "rejected", "Rejected"

    target_system = models.CharField(max_length=20, choices=SourceSystem.choices, default=SourceSystem.XERO)
    profile = models.CharField(max_length=100, help_text="Output profile used to build the file.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BUILT)
    file_name = models.CharField(max_length=255)
    file_path = models.CharField(max_length=1024, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    validation = models.JSONField(default=dict, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, help_text="Hash of the file bytes at stamp time.")
    built_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    stamped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    stamped_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)

    class Meta(TenantModel.Meta):
        indexes = [models.Index(fields=["organisation", "status"])]

    def __str__(self):
        return f"{self.file_name} ({self.get_status_display()})"

    @staticmethod
    def hash_bytes(content):
        return hashlib.sha256(content).hexdigest()

    def stamp(self, user, content):
        if self.status != self.Status.BUILT:
            raise ValidationError(f"Only built files can be stamped (status is {self.status}).")
        self.sha256 = self.hash_bytes(content)
        self.stamped_by = user
        self.stamped_at = timezone.now()
        self.status = self.Status.STAMPED
        self.save()

    def stamp_is_valid(self, content):
        return bool(self.sha256) and self.hash_bytes(content) == self.sha256


class AuditLog(TenantModel):
    """Append-only log of every command, approval, stamp and override."""

    class Action(models.TextChoices):
        COMMAND = "command", "Command"
        APPROVAL = "approval", "Approval"
        STAMP = "stamp", "Stamp"
        OVERRIDE = "override", "Override"
        CONNECT = "connect", "Connect"
        DISCONNECT = "disconnect", "Disconnect"
        SYNC = "sync", "Sync"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=20, choices=Action.choices)
    input = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    succeeded = models.BooleanField(default=True)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=255, blank=True)

    class Meta(TenantModel.Meta):
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organisation", "created_at"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Audit log entries cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit log entries cannot be deleted.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.input[:40]}"
