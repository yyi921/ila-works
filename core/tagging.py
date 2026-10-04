"""Writing and checking dimension tags, and splitting amounts by allocation."""

from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import BankTransactionLine, DocumentLine, JournalLine, Tag

ONE = Decimal("1")
CENT = Decimal("0.01")

LINE_FIELDS = {
    DocumentLine: "document_line",
    BankTransactionLine: "bank_transaction_line",
    JournalLine: "journal_line",
}


def _line_field(line):
    try:
        return LINE_FIELDS[type(line)]
    except KeyError:
        raise TypeError(f"{type(line).__name__} cannot be tagged") from None


@transaction.atomic
def set_line_tags(line, dimension, allocations, *, method, confidence, set_by=None, status=Tag.Status.PROPOSED,
                  rule="", note=""):
    """Replace the line's live tags in ``dimension`` with ``allocations``: a list of (DimensionValue, Decimal)
    whose shares must sum to exactly 1. Previous live tags are kept as history with status superseded.
    Pass an empty list to clear the line back to Unassigned."""
    field = _line_field(line)
    allocations = [(value, Decimal(share)) for value, share in allocations]
    for value, share in allocations:
        if value.dimension_id != dimension.pk:
            raise ValidationError(f"{value} is not a value of dimension {dimension}.")
        if value.organisation_id != line.organisation_id:
            raise ValidationError(f"{value} belongs to another organisation.")
        if not ONE >= share > 0:
            raise ValidationError(f"Allocation {share} for {value} must be greater than 0 and at most 1.")
    if allocations and sum(share for _, share in allocations) != ONE:
        raise ValidationError(f"Allocations sum to {sum(s for _, s in allocations)}, not 1.")
    if len({value.pk for value, _ in allocations}) != len(allocations):
        raise ValidationError("Each dimension value may appear only once per line.")

    live = Tag.objects.select_for_update().filter(**{field: line}, dimension=dimension).exclude(status__in=Tag.NOT_LIVE)
    live.update(status=Tag.Status.SUPERSEDED)
    return [
        Tag.objects.create(
            organisation_id=line.organisation_id, **{field: line}, dimension_value=value, allocation=share,
            method=method, confidence=confidence, status=status, set_by=set_by, rule=rule, note=note,
        )
        for value, share in allocations
    ]


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
