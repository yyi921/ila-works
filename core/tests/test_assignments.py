from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models import (
    Account,
    Assignment,
    Dimension,
    DimensionValue,
    Document,
    DocumentLine,
    Organisation,
    Tag,
    TrackingCategory,
    TrackingOption,
)
from core.tagging import (
    apply_assignments,
    assign,
    assign_from_tracking,
    assign_group,
    reject,
    tracking_mismatches,
    unbalanced_lines,
)

D = Decimal
RULE = {"method": Tag.Method.RULE, "confidence": Tag.Confidence.MEDIUM}
MANUAL = {"method": Tag.Method.MANUAL, "confidence": Tag.Confidence.HIGH, "status": Tag.Status.CONFIRMED}


@pytest.fixture
def org(db):
    return Organisation.objects.create(name="Demo Co")


@pytest.fixture
def account(org):
    return Account.objects.create(organisation=org, code="400", name="Catering",
                                  account_class=Account.AccountClass.EXPENSE)


def make_bill(org, account, amounts=("60.00", "40.00"), number="BILL-1"):
    bill = Document.objects.create(organisation=org, type=Document.Type.BILL, ledger=Document.Ledger.PAYABLE,
                                   status=Document.Status.AUTHORISED, number=number)
    for i, amount in enumerate(amounts, 1):
        DocumentLine.objects.create(organisation=org, document=bill, account=account, line_number=i,
                                    line_amount=D(amount), source_hash=f"h{i}")
    return bill


@pytest.fixture
def bill(org, account):
    return make_bill(org, account)


@pytest.fixture
def tracking(org):
    category = TrackingCategory.objects.create(organisation=org, source_system="xero", source_id="tc-1", name="Event")
    expo, gala = (TrackingOption.objects.create(organisation=org, source_system="xero", source_id=f"to-{n}",
                                                category=category, name=n) for n in ("Expo", "Gala"))
    return category, expo, gala


@pytest.fixture
def events(org, tracking):
    category, expo_opt, gala_opt = tracking
    dim = Dimension.objects.create(organisation=org, code="event", name="Event", tracking_category=category)
    expo = DimensionValue.objects.create(organisation=org, dimension=dim, code="EXPO", name="Expo",
                                         tracking_option=expo_opt)
    gala = DimensionValue.objects.create(organisation=org, dimension=dim, code="GALA", name="Gala",
                                         tracking_option=gala_opt)
    smith = DimensionValue.objects.create(organisation=org, dimension=dim, code="SMITH", name="Smith wedding")
    return dim, expo, gala, smith


def tags_by_line(document, dim):
    return {
        line.line_number: sorted((t.dimension_value.code, t.allocation)
                                 for t in line.tags.filter(dimension=dim).exclude(status__in=Tag.NOT_LIVE))
        for line in document.lines.all()
    }


def test_transaction_level_tags_every_line(bill, events):
    dim, _, gala, _ = events
    a = assign(bill, dim, [(gala, D("1"))], **MANUAL)
    assert a.level == Assignment.Level.TRANSACTION
    assert tags_by_line(bill, dim) == {1: [("GALA", D("1"))], 2: [("GALA", D("1"))]}


def test_transaction_level_split_splits_each_line(bill, events):
    dim, expo, gala, _ = events
    assign(bill, dim, [(expo, D("0.25")), (gala, D("0.75"))], **MANUAL)
    assert tags_by_line(bill, dim)[1] == [("EXPO", D("0.25")), ("GALA", D("0.75"))]
    assert unbalanced_lines(bill.organisation, dim) == []


def test_transaction_level_covers_lines_added_later_and_survives_edits(bill, events, account):
    dim, _, gala, _ = events
    assign(bill, dim, [(gala, D("1"))], **MANUAL)
    DocumentLine.objects.create(organisation=bill.organisation, document=bill, account=account, line_number=3,
                                line_amount=D("5.00"), source_hash="h3")
    bill.lines.filter(line_number=1).update(source_hash="h1-edited")
    apply_assignments(bill)

    assert tags_by_line(bill, dim)[3] == [("GALA", D("1"))]
    assert not any(t.is_stale for t in Tag.objects.filter(dimension=dim, status=Tag.Status.CONFIRMED))


def test_line_level_tag_goes_stale_when_line_changes(bill, events):
    dim, expo, *_ = events
    line = bill.lines.get(line_number=1)
    assign(line, dim, [(expo, D("1"))], **MANUAL)
    bill.lines.filter(pk=line.pk).update(source_hash="h1-edited")
    apply_assignments(bill)
    assert Tag.objects.get(document_line=line, status=Tag.Status.CONFIRMED).is_stale


def test_line_overrides_transaction_at_same_authority(bill, events):
    dim, expo, gala, _ = events
    assign(bill, dim, [(gala, D("1"))], **MANUAL)
    assign(bill.lines.get(line_number=2), dim, [(expo, D("1"))], **MANUAL)
    assert tags_by_line(bill, dim) == {1: [("GALA", D("1"))], 2: [("EXPO", D("1"))]}


def test_manual_transaction_beats_rule_on_a_line(bill, events):
    dim, expo, gala, _ = events
    assign(bill.lines.get(line_number=1), dim, [(expo, D("1"))], **RULE)
    assign(bill, dim, [(gala, D("1"))], **MANUAL)
    assert tags_by_line(bill, dim)[1] == [("GALA", D("1"))]


def test_manual_unassigned_line_overrides_transaction_tag(bill, events):
    dim, _, gala, _ = events
    assign(bill, dim, [(gala, D("1"))], **MANUAL)
    assign(bill.lines.get(line_number=2), dim, [], **MANUAL)
    assert tags_by_line(bill, dim) == {1: [("GALA", D("1"))], 2: []}


def test_reject_falls_back_to_next_assignment(bill, events):
    dim, expo, gala, _ = events
    assign(bill, dim, [(expo, D("1"))], **RULE)
    manual = assign(bill, dim, [(gala, D("1"))], **MANUAL)
    reject(manual)
    assert tags_by_line(bill, dim) == {1: [("EXPO", D("1"))], 2: [("EXPO", D("1"))]}
    reject(Assignment.objects.get(method=Tag.Method.RULE))
    assert tags_by_line(bill, dim) == {1: [], 2: []}


def test_reassign_supersedes_same_method_only(bill, events):
    dim, expo, gala, _ = events
    rule = assign(bill, dim, [(expo, D("1"))], **RULE)
    first = assign(bill, dim, [(gala, D("1"))], **MANUAL)
    assign(bill, dim, [(expo, D("1"))], **MANUAL)
    first.refresh_from_db()
    rule.refresh_from_db()
    assert first.status == Tag.Status.SUPERSEDED
    assert rule.status == Tag.Status.PROPOSED


def test_group_tags_several_transactions_with_one_batch(org, account, bill, events):
    dim, _, _, smith = events
    final = make_bill(org, account, ("250.00",), number="BILL-2")
    assignments = assign_group([bill, final], dim, [(smith, D("1"))], **MANUAL)
    assert len({a.batch for a in assignments}) == 1 and assignments[0].batch
    assert tags_by_line(final, dim) == {1: [("SMITH", D("1"))]}
    assert tags_by_line(bill, dim)[2] == [("SMITH", D("1"))]


def test_rejects_bad_allocations_and_other_org_values(bill, events):
    dim, expo, gala, _ = events
    with pytest.raises(ValidationError):
        assign(bill, dim, [(expo, D("0.5")), (gala, D("0.4"))], **MANUAL)
    other = Organisation.objects.create(name="Other")
    other_dim = Dimension.objects.create(organisation=other, code="event", name="Event")
    with pytest.raises(ValidationError):
        assign(bill, other_dim, [], **MANUAL)
    assert not Assignment.objects.exists()


def test_db_requires_exactly_one_target(bill, events):
    dim, *_ = events
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.bulk_create([Assignment(organisation=bill.organisation, dimension=dim, level="line",
                                                   method=Tag.Method.MANUAL, confidence=Tag.Confidence.HIGH)])
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.bulk_create([Assignment(organisation=bill.organisation, dimension=dim, document=bill,
                                                   document_line=bill.lines.first(), level="line",
                                                   method=Tag.Method.MANUAL, confidence=Tag.Confidence.HIGH)])


# --- Xero tracking ------------------------------------------------------------------------------------------


def test_tracking_becomes_confirmed_line_assignments(bill, events, tracking):
    dim, *_ = events
    _, expo_opt, _ = tracking
    bill.lines.get(line_number=1).tracking.add(expo_opt)
    assert assign_from_tracking(bill, dim) == []
    assert tags_by_line(bill, dim) == {1: [("EXPO", D("1"))], 2: []}
    assert Tag.objects.get(dimension=dim, status=Tag.Status.CONFIRMED).method == Tag.Method.TRACKING

    assign_from_tracking(bill, dim)  # unchanged: no new assignment
    assert Assignment.objects.filter(method=Tag.Method.TRACKING).count() == 1


def test_tracking_removed_in_source_drops_its_assignment(bill, events, tracking):
    dim, *_ = events
    _, expo_opt, _ = tracking
    line = bill.lines.get(line_number=1)
    line.tracking.add(expo_opt)
    assign_from_tracking(bill, dim)
    line.tracking.clear()
    assign_from_tracking(bill, dim)
    assert tags_by_line(bill, dim)[1] == []


def test_unmapped_tracking_option_is_reported(org, bill, events, tracking):
    dim, *_ = events
    category, _, _ = tracking
    new_opt = TrackingOption.objects.create(organisation=org, category=category, name="Fringe")
    line = bill.lines.get(line_number=1)
    line.tracking.add(new_opt)
    assert assign_from_tracking(bill, dim) == [line]


def test_ila_can_override_tracking_and_flags_the_mismatch(bill, events, tracking):
    dim, expo, gala, _ = events
    _, expo_opt, _ = tracking
    for line in bill.lines.all():
        line.tracking.add(expo_opt)
    assign_from_tracking(bill, dim)
    assert tracking_mismatches(bill.organisation, dim) == []

    assign(bill.lines.get(line_number=2), dim, [(gala, D("1"))], **MANUAL)
    assert tags_by_line(bill, dim) == {1: [("EXPO", D("1"))], 2: [("GALA", D("1"))]}
    [(line, ila, source)] = tracking_mismatches(bill.organisation, dim)
    assert line.line_number == 2 and ila == [(gala, D("1"))] and source == expo


def test_split_or_untracked_lines_are_mismatches(bill, events):
    dim, expo, gala, _ = events
    assign(bill.lines.get(line_number=1), dim, [(expo, D("0.5")), (gala, D("0.5"))], **MANUAL)
    assign(bill.lines.get(line_number=2), dim, [(gala, D("1"))], **MANUAL)
    assert {line.line_number for line, _, _ in tracking_mismatches(bill.organisation, dim)} == {1, 2}


# --- Custom attributes --------------------------------------------------------------------------------------


@pytest.fixture
def campaign(org):
    dim = Dimension(organisation=org, code="campaign", name="Campaign", attribute_fields=[
        {"key": "budget", "label": "Budget", "type": "number", "required": True},
        {"key": "launch", "label": "Launch date", "type": "date"},
        {"key": "owner", "label": "Owner", "type": "text"},
        {"key": "paid", "label": "Paid media", "type": "boolean"},
    ])
    dim.full_clean()
    dim.save()
    return dim


def test_valid_attributes_pass(org, campaign):
    value = DimensionValue(organisation=org, dimension=campaign, code="SPRING", name="Spring",
                           attributes={"budget": 5000, "launch": "2026-10-01", "owner": "Sam", "paid": True})
    value.full_clean()


@pytest.mark.parametrize("attributes, message", [
    ({}, "Budget is required."),
    ({"budget": "lots"}, "Budget must be a number value."),
    ({"budget": 1, "launch": "1/10/2026"}, "Launch date must be a date value."),
    ({"budget": 1, "paid": "yes"}, "Paid media must be a boolean value."),
    ({"budget": 1, "colour": "red"}, "Unknown field 'colour'."),
])
def test_invalid_attributes_rejected(org, campaign, attributes, message):
    value = DimensionValue(organisation=org, dimension=campaign, code="X", name="X", attributes=attributes)
    with pytest.raises(ValidationError) as exc:
        value.full_clean()
    assert message in exc.value.message_dict["attributes"]


@pytest.mark.parametrize("fields", [
    [{"label": "No key"}],
    [{"key": "a"}, {"key": "a"}],
    [{"key": "a", "type": "currency"}],
    {"key": "a"},
])
def test_bad_attribute_field_definitions_rejected(org, fields):
    dim = Dimension(organisation=org, code="dept", name="Department", attribute_fields=fields)
    with pytest.raises(ValidationError):
        dim.full_clean()
