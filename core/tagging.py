"""Dimension tagging: assignments (decisions at line or transaction level), the per-line tags derived from them,
and splitting amounts by allocation.

Flow: ``assign`` / ``assign_group`` / ``assign_from_tracking`` record a decision, then ``apply_assignments``
works out the winning assignment for every line of the affected transaction and rewrites that line's tags.
Sync must call ``apply_assignments(transaction)`` after it updates a transaction, so lines added in the source
pick up transaction-level assignments.
"""

import uuid
from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from .models import (
    Assignment,
    AssignmentShare,
    BankTransaction,
    BankTransactionLine,
    DimensionValue,
    Document,
    DocumentLine,
    Journal,
    JournalLine,
    Tag,
)

ONE = Decimal("1")
CENT = Decimal("0.01")

LINE_FIELDS = {
    DocumentLine: "document_line",
    BankTransactionLine: "bank_transaction_line",
    JournalLine: "journal_line",
}
# Transaction model -> (its field on Assignment, line model, the line's foreign key to the transaction).
PARENTS = {
    Document: ("document", DocumentLine, "document"),
    BankTransaction: ("bank_transaction", BankTransactionLine, "bank_transaction"),
    Journal: ("journal", JournalLine, "journal"),
}
PARENT_OF_LINE = {line_model: (parent, fk) for parent, (_, line_model, fk) in PARENTS.items()}

# Higher wins. Ties go to line level over transaction level, then to the newest.
AUTHORITY = {Tag.Method.MANUAL: 3, Tag.Method.IMPORT: 2, Tag.Method.TRACKING: 1, Tag.Method.RULE: 0}


def _line_field(line):
    try:
        return LINE_FIELDS[type(line)]
    except KeyError:
        raise TypeError(f"{type(line).__name__} cannot be tagged") from None


def _target_field(target):
    if type(target) in LINE_FIELDS:
        return LINE_FIELDS[type(target)]
    if type(target) in PARENTS:
        return PARENTS[type(target)][0]
    raise TypeError(f"{type(target).__name__} cannot be tagged")


def _parent(target):
    """The transaction a line belongs to, or the transaction itself."""
    if type(target) in PARENTS:
        return target
    return getattr(target, PARENT_OF_LINE[type(target)][1])


def _check_allocations(organisation_id, dimension, allocations):
    allocations = [(value, Decimal(share)) for value, share in allocations]
    for value, share in allocations:
        if value.dimension_id != dimension.pk:
            raise ValidationError(f"{value} is not a value of dimension {dimension}.")
        if value.organisation_id != organisation_id:
            raise ValidationError(f"{value} belongs to another organisation.")
        if not ONE >= share > 0:
            raise ValidationError(f"Allocation {share} for {value} must be greater than 0 and at most 1.")
    if allocations and sum(share for _, share in allocations) != ONE:
        raise ValidationError(f"Allocations sum to {sum(s for _, s in allocations)}, not 1.")
    if len({value.pk for value, _ in allocations}) != len(allocations):
        raise ValidationError("Each dimension value may appear only once per assignment.")
    return allocations


def _live(queryset):
    return queryset.exclude(status__in=Tag.NOT_LIVE)


@transaction.atomic
def assign(target, dimension, allocations, *, method, confidence, set_by=None, status=Tag.Status.PROPOSED,
           rule="", note="", batch=None, apply=True):
    """Record that ``target`` (a line, or a whole document, bank transaction or journal) belongs to
    ``allocations``: a list of (DimensionValue, Decimal) whose shares sum to exactly 1. An empty list means
    "Unassigned", which still overrides lower-precedence assignments (e.g. a manual "not part of any event" on
    one line of a tagged invoice). Supersedes the target's previous live assignment in this dimension made by the
    same method, then re-applies tags across the transaction. Returns the new Assignment."""
    field = _target_field(target)
    if dimension.organisation_id != target.organisation_id:
        raise ValidationError(f"Dimension {dimension} belongs to another organisation.")
    allocations = _check_allocations(target.organisation_id, dimension, allocations)

    _live(Assignment.objects.select_for_update().filter(**{field: target}, dimension=dimension, method=method)
          ).update(status=Tag.Status.SUPERSEDED)
    assignment = Assignment.objects.create(
        organisation_id=target.organisation_id, **{field: target}, dimension=dimension, method=method,
        confidence=confidence, status=status, set_by=set_by, rule=rule, note=note, batch=batch,
    )
    AssignmentShare.objects.bulk_create(
        AssignmentShare(assignment=assignment, dimension_value=value, allocation=share) for value, share in allocations
    )
    if apply:
        apply_assignments(_parent(target), [dimension])
    return assignment


@transaction.atomic
def assign_group(transactions, dimension, allocations, **kwargs):
    """Tag several whole transactions (documents, bank transactions, journals) with the same allocations, e.g. the
    deposit invoice, final invoice and supplier bills of one event. The assignments share a ``batch`` id."""
    batch = kwargs.pop("batch", None) or uuid.uuid4()
    return [assign(txn, dimension, allocations, batch=batch, **kwargs) for txn in transactions]


@transaction.atomic
def reject(assignment, *, set_by=None):
    """Reject an assignment; its lines fall back to the next assignment by precedence, or Unassigned."""
    assignment.status = Tag.Status.REJECTED
    if set_by is not None:
        assignment.set_by = set_by
    assignment.save(update_fields=["status", "set_by", "updated_at"])
    apply_assignments(_parent(assignment.target), [assignment.dimension])


def set_line_tags(line, dimension, allocations, **kwargs):
    """Assign one line directly and return its resulting live tags in ``dimension``."""
    _line_field(line)
    assign(line, dimension, allocations, **kwargs)
    return list(_live(line.tags.filter(dimension=dimension)).order_by("pk"))


def _precedence(assignment):
    return (AUTHORITY[assignment.method], assignment.level == Assignment.Level.LINE, assignment.set_at,
            assignment.pk)


@transaction.atomic
def apply_assignments(parent, dimensions=None):
    """Rewrite the live tags of every line of ``parent`` (a document, bank transaction or journal) from the
    winning assignment per line and dimension. ``dimensions`` defaults to every dimension that has an assignment
    or a live tag on the transaction. Lines whose winner is unchanged keep their tags; for a transaction-level
    winner the tag's line hash is refreshed, since the decision covers the transaction whatever its lines say."""
    txn_field, line_model, _ = PARENTS[type(parent)]
    line_field = LINE_FIELDS[line_model]
    lines = list(parent.lines.all())
    covering = Assignment.objects.filter(Q(**{txn_field: parent}) | Q(**{f"{line_field}__in": lines}))
    if dimensions is None:
        dimension_ids = set(covering.values_list("dimension_id", flat=True))
        dimension_ids |= set(Tag.objects.filter(**{f"{line_field}__in": lines}).values_list("dimension_id", flat=True))
    else:
        dimension_ids = {d.pk for d in dimensions}

    for dimension_id in dimension_ids:
        live = list(_live(covering.filter(dimension_id=dimension_id)).prefetch_related("shares"))
        tags_by_line = {}
        for tag in _live(Tag.objects.select_for_update().filter(**{f"{line_field}__in": lines},
                                                                dimension_id=dimension_id)):
            tags_by_line.setdefault(getattr(tag, f"{line_field}_id"), []).append(tag)

        for line in lines:
            candidates = [a for a in live if getattr(a, f"{line_field}_id") in (None, line.pk)]
            winner = max(candidates, key=_precedence, default=None)
            wanted = {(s.dimension_value_id, s.allocation) for s in winner.shares.all()} if winner else set()
            current = tags_by_line.get(line.pk, [])

            if winner and current and {t.assignment_id for t in current} == {winner.pk} and \
                    {(t.dimension_value_id, t.allocation) for t in current} == wanted:
                if winner.level == Assignment.Level.TRANSACTION:
                    for tag in current:
                        if tag.line_source_hash != line.source_hash or tag.status != winner.status:
                            tag.line_source_hash, tag.status = line.source_hash, winner.status
                            tag.save(update_fields=["line_source_hash", "status", "updated_at"])
                continue
            if not winner and not current:
                continue

            Tag.objects.filter(pk__in=[t.pk for t in current]).update(status=Tag.Status.SUPERSEDED)
            for share in winner.shares.all() if winner else ():
                Tag.objects.create(
                    organisation_id=line.organisation_id, **{line_field: line}, dimension_value_id=share.dimension_value_id,
                    allocation=share.allocation, assignment=winner, method=winner.method, rule=winner.rule,
                    confidence=winner.confidence, status=winner.status, set_by=winner.set_by, note=winner.note,
                )


@transaction.atomic
def assign_from_tracking(parent, dimension):
    """Mirror the source's tracking on each line of ``parent`` as line-level tracking assignments in
    ``dimension`` (which must be linked to a tracking category). Lines whose tracking is unchanged are left alone;
    lines whose tracking was removed in the source lose their tracking assignment. Returns the lines whose
    tracking option has no matching dimension value, for ``dims import`` to resolve."""
    if not dimension.tracking_category_id:
        raise ValidationError(f"Dimension {dimension} is not linked to a tracking category.")
    values = {v.tracking_option_id: v for v in dimension.values.filter(tracking_option__isnull=False)}
    line_field = LINE_FIELDS[PARENTS[type(parent)][1]]
    unmapped = []
    for line in parent.lines.prefetch_related("tracking"):
        option = next((o for o in line.tracking.all() if o.category_id == dimension.tracking_category_id), None)
        existing = _live(Assignment.objects.filter(**{line_field: line}, dimension=dimension,
                                                   method=Tag.Method.TRACKING)).first()
        if option is None:
            if existing:
                existing.status = Tag.Status.SUPERSEDED
                existing.save(update_fields=["status", "updated_at"])
            continue
        value = values.get(option.pk)
        if value is None:
            unmapped.append(line)
            continue
        if existing and [(s.dimension_value_id, s.allocation) for s in existing.shares.all()] == [(value.pk, ONE)]:
            continue
        assign(line, dimension, [(value, ONE)], method=Tag.Method.TRACKING, confidence=Tag.Confidence.HIGH,
               status=Tag.Status.CONFIRMED, apply=False)
    apply_assignments(parent, [dimension])
    return unmapped


def tracking_mismatches(organisation, dimension):
    """Lines where iLA's live tags in a tracking-linked ``dimension`` differ from the source's tracking: tagged in
    iLA but not in the source, tracked in the source but tagged differently (or Unassigned) in iLA, or split in
    iLA (the source holds one option per line). Returns (line, ila_values, source_value) tuples, where
    ila_values is a list of (DimensionValue, allocation) and source_value is a DimensionValue or None. These are
    the lines to correct in the source via a journal or import file."""
    if not dimension.tracking_category_id:
        return []
    by_option = {v.tracking_option_id: v for v in dimension.values.filter(tracking_option__isnull=False)}
    mismatches = []
    for line_model, field in LINE_FIELDS.items():
        lines = line_model.objects.filter(organisation=organisation).filter(
            Q(tags__dimension=dimension) | Q(tracking__category_id=dimension.tracking_category_id)
        ).distinct().prefetch_related("tracking")
        for line in lines:
            tags = _live(line.tags.filter(dimension=dimension)).select_related("dimension_value")
            ila = sorted(((t.dimension_value, t.allocation) for t in tags), key=lambda pair: pair[0].pk)
            option = next((o for o in line.tracking.all() if o.category_id == dimension.tracking_category_id), None)
            source = by_option.get(option.pk) if option else None
            if option and source is None:
                source = DimensionValue(dimension=dimension, code=option.name, name=f"{option.name} (not imported)")
            matches = (not ila and source is None) or (
                source is not None and source.pk and len(ila) == 1 and ila[0][0].pk == source.pk)
            if not matches:
                mismatches.append((line, ila, source))
    return mismatches


def unbalanced_lines(organisation, dimension):
    """Lines whose live tags in ``dimension`` don't sum to 1, as (line field, line id, total) tuples.
    Should always be empty; run it before reporting by dimension."""
    live = Tag.objects.filter(organisation=organisation, dimension=dimension).exclude(status__in=Tag.NOT_LIVE)
    problems = []
    for field in LINE_FIELDS.values():
        rows = (live.filter(**{f"{field}__isnull": False}).values(field).annotate(total=Sum("allocation"))
                .exclude(total=ONE))
        problems += [(field, row[field], row["total"]) for row in rows]
    return problems


def split_amount(amount, allocations):
    """Split ``amount`` by ``allocations`` (Decimals summing to 1) into cent amounts that sum exactly to
    ``amount``, using the largest-remainder method: truncate each share to the cent, then hand the leftover
    cents to the shares with the largest truncated remainders (earliest first on ties)."""
    amount = Decimal(amount)
    sign = -1 if amount < 0 else 1
    exact = [abs(amount) * Decimal(share) for share in allocations]
    parts = [e.quantize(CENT, rounding=ROUND_DOWN) for e in exact]
    leftover_cents = int((abs(amount) - sum(parts)) / CENT)
    by_remainder = sorted(range(len(parts)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in by_remainder[:leftover_cents]:
        parts[i] += CENT
    return [sign * p for p in parts]
