from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models import (
    Account,
    AuditLog,
    Contact,
    Dimension,
    DimensionValue,
    Document,
    DocumentLine,
    ImportFile,
    Organisation,
    SourceSystem,
    Tag,
)


@pytest.fixture
def org(db):
    return Organisation.objects.create(name="Demo Co")


@pytest.fixture
def line(org):
    account = Account.objects.create(organisation=org, source_system="xero", source_id="acc-1", code="400",
                                     name="Catering", account_class=Account.AccountClass.EXPENSE)
    bill = Document.objects.create(organisation=org, source_system="xero", source_id="inv-1",
                                   type=Document.Type.BILL, ledger=Document.Ledger.PAYABLE,
                                   status=Document.Status.AUTHORISED, total=110)
    return DocumentLine.objects.create(organisation=org, source_system="xero", source_id="li-1", document=bill,
                                       account=account, line_amount=100, source_hash="h1")


@pytest.fixture
def event_value(org):
    dim = Dimension.objects.create(organisation=org, code="event", name="Event")
    return DimensionValue.objects.create(organisation=org, dimension=dim, code="EV1", name="Gala")


def test_source_id_unique_per_org_and_system(org):
    Contact.objects.create(organisation=org, source_system="xero", source_id="c-1", name="Acme")
    Contact.objects.create(organisation=org, source_system="import", source_id="c-1", name="Acme (CSV)")
    Contact.objects.create(organisation=Organisation.objects.create(name="Other"), source_system="xero",
                           source_id="c-1", name="Acme")
    with pytest.raises(IntegrityError):
        Contact.objects.create(organisation=org, source_system="xero", source_id="c-1", name="Dupe")


def test_blank_source_id_allowed_for_ila_rows(org):
    Dimension.objects.create(organisation=org, code="dept", name="Department")
    Dimension.objects.create(organisation=org, code="venue", name="Venue")
    assert Dimension.objects.for_organisation(org).filter(source_system=SourceSystem.ILA).count() == 2


def test_value_dates_and_window(org):
    dim = Dimension.objects.create(organisation=org, code="event", name="Event")
    gala = DimensionValue.objects.create(organisation=org, dimension=dim, code="261107GAL", name="Gala",
                                         start_date=date(2026, 11, 7))
    assert gala.last_date == date(2026, 11, 7) and not gala.is_season
    assert gala.window() == (date(2026, 11, 5), date(2026, 11, 7), date(2026, 11, 7), date(2026, 11, 9))
    gala.bump_in_days, gala.bump_out_days = 0, 1
    assert gala.window() == (date(2026, 11, 7), date(2026, 11, 7), date(2026, 11, 7), date(2026, 11, 8))
    season = DimensionValue(organisation=org, dimension=dim, code="2627FIRE", name="Fire season",
                            start_date=date(2026, 10, 1), end_date=date(2027, 2, 28))
    assert season.is_season
    season.season = False
    assert not season.is_season
    assert DimensionValue(organisation=org, dimension=dim, code="X", name="No dates").window() is None
    with pytest.raises(ValidationError, match="before the start"):
        DimensionValue(organisation=org, dimension=dim, code="BAD", name="Bad", start_date=date(2026, 11, 7),
                       end_date=date(2026, 11, 6)).full_clean()
    with pytest.raises(ValidationError, match="start as well"):
        DimensionValue(organisation=org, dimension=dim, code="BAD", name="Bad", end_date=date(2026, 11, 6)).full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        DimensionValue.objects.create(organisation=org, dimension=dim, code="BAD", name="Bad",
                                      start_date=date(2026, 11, 7), end_date=date(2026, 11, 6))


def test_tag_sets_dimension_and_snapshots_line_hash(line, event_value):
    tag = Tag.objects.create(organisation=line.organisation, document_line=line, dimension_value=event_value,
                             method=Tag.Method.RULE, confidence=Tag.Confidence.HIGH)
    assert tag.dimension == event_value.dimension
    assert tag.line_source_hash == "h1"
    assert not tag.is_stale

    line.source_hash = "h2"
    line.save()
    tag.refresh_from_db()
    assert tag.is_stale


def test_tag_requires_exactly_one_line(org, event_value):
    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.bulk_create([Tag(organisation=org, dimension=event_value.dimension, dimension_value=event_value,
                                     method=Tag.Method.MANUAL, confidence=Tag.Confidence.HIGH)])


def test_import_file_stamp_invalidated_by_edit(org, django_user_model):
    user = django_user_model.objects.create_user(username="erin")
    f = ImportFile.objects.create(organisation=org, profile="xero-manual-journal", file_name="jnl.csv", built_by=user)
    content = b"date,account,amount\n2026-10-31,400,100.00\n"

    f.stamp(user, content)

    assert f.status == ImportFile.Status.STAMPED
    assert f.stamped_by == user and f.stamped_at
    assert f.stamp_is_valid(content)
    assert not f.stamp_is_valid(content.replace(b"100.00", b"1000.00"))
    with pytest.raises(ValidationError):
        f.stamp(user, content)


def test_audit_log_is_append_only(org):
    entry = AuditLog.objects.create(organisation=org, action=AuditLog.Action.COMMAND, input="bal 400")
    entry.input = "edited"
    with pytest.raises(ValidationError):
        entry.save()
    with pytest.raises(ValidationError):
        entry.delete()
