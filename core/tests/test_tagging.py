from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models import Account, Dimension, DimensionValue, Document, DocumentLine, Organisation, Tag
from core.tagging import set_line_tags, split_amount, unbalanced_lines

D = Decimal
TAG_KW = {"method": Tag.Method.RULE, "confidence": Tag.Confidence.MEDIUM}


@pytest.fixture
def org(db):
    return Organisation.objects.create(name="Demo Co")


@pytest.fixture
def line(org):
    account = Account.objects.create(organisation=org, code="477", name="Wages",
                                     account_class=Account.AccountClass.EXPENSE)
    bill = Document.objects.create(organisation=org, type=Document.Type.BILL, ledger=Document.Ledger.PAYABLE,
                                   status=Document.Status.AUTHORISED, total=D("100.00"))
    return DocumentLine.objects.create(organisation=org, document=bill, account=account, line_amount=D("100.00"))


@pytest.fixture
def events(org):
    dim = Dimension.objects.create(organisation=org, code="event", name="Event")
    return dim, [DimensionValue.objects.create(organisation=org, dimension=dim, code=f"EV{i}", name=f"Event {i}")
                 for i in range(1, 4)]


def live_tags(line):
    return line.tags.exclude(status__in=Tag.NOT_LIVE)


def test_allocation_defaults_to_whole_line(line, events):
    dim, (ev1, *_) = events
    tag = Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=ev1, **TAG_KW)
    assert tag.allocation == D("1")


def test_split_across_two_events(line, events):
    dim, (ev1, ev2, _) = events
    tags = set_line_tags(line, dim, [(ev1, D("0.6")), (ev2, D("0.4"))], **TAG_KW)
    assert [t.allocation for t in tags] == [D("0.6"), D("0.4")]
    assert unbalanced_lines(line.organisation, dim) == []


@pytest.mark.parametrize("shares", [[D("0.6"), D("0.3")], [D("0.6"), D("0.6")]])
def test_rejects_allocations_not_summing_to_one(line, events, shares):
    dim, values = events
    with pytest.raises(ValidationError):
        set_line_tags(line, dim, list(zip(values, shares)), **TAG_KW)
    assert not line.tags.exists()


def test_rejects_value_from_another_dimension(line, events, org):
    dim, _ = events
    dept = Dimension.objects.create(organisation=org, code="dept", name="Department")
    kitchen = DimensionValue.objects.create(organisation=org, dimension=dept, code="K", name="Kitchen")
    with pytest.raises(ValidationError):
        set_line_tags(line, dim, [(kitchen, D("1"))], **TAG_KW)


def test_retag_supersedes_previous_split(line, events):
    dim, (ev1, ev2, ev3) = events
    set_line_tags(line, dim, [(ev1, D("0.5")), (ev2, D("0.5"))], **TAG_KW)
    set_line_tags(line, dim, [(ev3, D("1"))], method=Tag.Method.MANUAL, confidence=Tag.Confidence.HIGH)

    assert [t.dimension_value for t in live_tags(line)] == [ev3]
    assert line.tags.filter(status=Tag.Status.SUPERSEDED).count() == 2


def test_empty_allocations_clears_to_unassigned(line, events):
    dim, (ev1, *_) = events
    set_line_tags(line, dim, [(ev1, D("1"))], **TAG_KW)
    set_line_tags(line, dim, [], **TAG_KW)
    assert not live_tags(line).exists()


def test_splits_in_different_dimensions_are_independent(line, events, org):
    dim, (ev1, ev2, _) = events
    dept = Dimension.objects.create(organisation=org, code="dept", name="Department")
    foh, boh = (DimensionValue.objects.create(organisation=org, dimension=dept, code=c, name=c) for c in ("FOH", "BOH"))
    set_line_tags(line, dim, [(ev1, D("0.5")), (ev2, D("0.5"))], **TAG_KW)
    set_line_tags(line, dept, [(foh, D("0.25")), (boh, D("0.75"))], **TAG_KW)
    assert live_tags(line).count() == 4
    assert unbalanced_lines(org, dim) == unbalanced_lines(org, dept) == []


def test_unbalanced_lines_detects_direct_writes(line, events):
    dim, (ev1, ev2, _) = events
    Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=ev1,
                       allocation=D("0.5"), **TAG_KW)
    assert unbalanced_lines(line.organisation, dim) == [("document_line", line.pk, D("0.5"))]


def test_db_rejects_out_of_range_allocation(line, events):
    dim, (ev1, *_) = events
    for bad in (D("0"), D("1.5")):
        with pytest.raises(IntegrityError), transaction.atomic():
            Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=ev1,
                               allocation=bad, **TAG_KW)


def test_db_rejects_same_value_twice_on_a_line(line, events):
    dim, (ev1, *_) = events
    Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=ev1,
                       allocation=D("0.5"), **TAG_KW)
    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=ev1,
                           allocation=D("0.5"), **TAG_KW)


@pytest.mark.parametrize(
    "amount, shares, expected",
    [
        ("100.00", ["0.6", "0.4"], ["60.00", "40.00"]),
        ("100.00", ["0.333333", "0.333333", "0.333334"], ["33.33", "33.33", "33.34"]),
        ("0.01", ["0.5", "0.5"], ["0.01", "0.00"]),
        ("-55.55", ["0.5", "0.5"], ["-27.78", "-27.77"]),
        ("10.00", ["1"], ["10.00"]),
    ],
)
def test_split_amount_ties_to_the_cent(amount, shares, expected):
    parts = split_amount(D(amount), [D(s) for s in shares])
    assert parts == [D(e) for e in expected]
    assert sum(parts) == D(amount)
