"""The bucket of overlay journals: who sees which, versions (undo and redo), their effect on the accounts, the
checks, the Xero import file that turns a Draft into To be posted, and leaving the bucket once in Xero.

See docs/Overlay journals — Design.md. Nothing here writes to Xero.
"""

import csv
import io
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from .models import (
    Account,
    Adjustment,
    AdjustmentLine,
    AdjustmentVersion,
    AuditLog,
    ImportFile,
    Journal,
    SourceSystem,
    TrackingOption,
)

ZERO = Decimal("0")
DEFAULT_TAX = "BAS Excluded"
COLUMNS = ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount", "TrackingName1",
           "TrackingOption1", "TrackingName2", "TrackingOption2"]
LINE_FIELDS = ("account_code", "description", "amount", "tax_rate", "tracking_name1", "tracking_option1",
               "tracking_name2", "tracking_option2", "value_id")
DRAFT, TO_POST = Adjustment.Status.DRAFT, Adjustment.Status.TO_POST


class OverlayError(Exception):
    """Something a person can fix; the message is shown to them."""


# --- Who sees what ------------------------------------------------------------------------------------------------

def visible(org, user):
    """The journals ``user`` sees: everything To be posted, their own drafts, and iLA's drafts, except an iLA draft
    they have their own copy of (same origin and date)."""
    found = list(Adjustment.objects.filter(organisation=org)
                 .filter(Q(status=TO_POST) | Q(made_by=user) | Q(made_by__isnull=True))
                 .prefetch_related("lines").order_by("number"))
    taken = {(a.origin, a.origin_ref, a.date) for a in found if a.origin and not a.by_ila}
    return [a for a in found if not (a.by_ila and a.is_draft and (a.origin, a.origin_ref, a.date) in taken)]


def in_period(journals, start, end):
    """Those dated in the period, or reversing in it."""
    return [a for a in journals if start <= a.date <= end or (a.reverse_date and start <= a.reverse_date <= end)]


def effect(journals, start, end):
    """{account code: {"draft": amount, "to_post": amount}} for the period, debit positive: each journal dated in
    it, less each one reversing in it."""
    out = defaultdict(lambda: {DRAFT.value: ZERO, TO_POST.value: ZERO})
    for adj in journals:
        for sign, day in ((1, adj.date), (-1, adj.reverse_date)):
            if day and start <= day <= end:
                for line in adj.lines.all():
                    out[line.account_code][adj.status] += sign * line.amount
    return out


# --- Versions -----------------------------------------------------------------------------------------------------

def snapshot(adj):
    return {"date": adj.date.isoformat(), "period_end": adj.period_end.isoformat(), "narration": adj.narration,
            "reverse_date": adj.reverse_date.isoformat() if adj.reverse_date else None,
            "lines": [{f: (str(getattr(line, f)) if f == "amount" else getattr(line, f)) for f in LINE_FIELDS}
                      for line in adj.lines.order_by("line_number")]}


def _restore(adj, data):
    from datetime import date  # noqa: PLC0415

    adj.date, adj.period_end = date.fromisoformat(data["date"]), date.fromisoformat(data["period_end"])
    adj.narration = data["narration"]
    adj.reverse_date = date.fromisoformat(data["reverse_date"]) if data["reverse_date"] else None
    adj.lines.all().delete()
    AdjustmentLine.objects.bulk_create(
        AdjustmentLine(adjustment=adj, line_number=i, **{**line, "amount": Decimal(line["amount"])})
        for i, line in enumerate(data["lines"], 1))


def record(adj, user, what):
    """Keep the journal as it now stands as a new version (dropping any undone ones)."""
    adj.versions.filter(number__gt=adj.version).delete()
    adj.version += 1
    AdjustmentVersion.objects.create(adjustment=adj, number=adj.version, what=what[:255], snapshot=snapshot(adj),
                                     made_by=user)
    adj.save()


def go_to(adj, number):
    """Show version ``number`` (undo, redo, restore). The versions themselves are kept."""
    version = adj.versions.filter(number=number).first()
    if version is None:
        raise OverlayError(f"{adj.code} has no version {number}.")
    _restore(adj, version.snapshot)
    adj.version = number
    adj.save()
    return version


def renumber(adj):
    for i, line in enumerate(adj.lines.order_by("line_number", "pk"), 1):
        if line.line_number != i:
            line.line_number = i
            line.save(update_fields=["line_number"])


# --- Making and copying -------------------------------------------------------------------------------------------

def new(org, user, day, period_end, narration):
    adj = Adjustment.objects.create(organisation=org, made_by=user, date=day, period_end=period_end,
                                    narration=narration[:255])
    record(adj, user, "new")
    return adj


def copy(adj, user, *, sign=1, narration=None, keep_origin=False):
    """A draft of ``user``'s with the same lines (``sign`` -1 for the reverse)."""
    twin = Adjustment.objects.create(
        organisation=adj.organisation, made_by=user, date=adj.date, period_end=adj.period_end,
        narration=(narration or adj.narration)[:255], evidence=adj.evidence,
        origin=adj.origin if keep_origin else "", origin_ref=adj.origin_ref if keep_origin else "")
    AdjustmentLine.objects.bulk_create(
        AdjustmentLine(adjustment=twin, line_number=line.line_number,
                       **{f: (sign * line.amount if f == "amount" else getattr(line, f)) for f in LINE_FIELDS})
        for line in adj.lines.all())
    record(twin, user, f"copy of {adj.code}")
    return twin


# --- Checks and the import file -----------------------------------------------------------------------------------

def checks(adj):
    """What stops the import file being generated."""
    lines = list(adj.lines.all())
    problems = []
    if not lines:
        return ["It has no lines."]
    out = sum((line.amount for line in lines), ZERO)
    if out:
        problems.append(f"It doesn't balance: debits are {abs(out):,.2f} {'more' if out > 0 else 'less'} than "
                        "credits.")
    if any(not line.amount for line in lines):
        problems.append("A line has no amount.")
    codes = {line.account_code for line in lines}
    known = set(Account.objects.filter(organisation=adj.organisation, code__in=codes, is_active=True)
                .values_list("code", flat=True))
    if codes - known:
        problems.append("Not an account in Xero's chart: " + ", ".join(sorted(codes - known)) + ".")
    pairs = {(n, o) for line in lines for n, o in ((line.tracking_name1, line.tracking_option1),
                                                  (line.tracking_name2, line.tracking_option2)) if n or o}
    if pairs:
        options = {(c.lower(), o.lower()) for c, o in TrackingOption.objects.filter(
            organisation=adj.organisation, is_active=True).values_list("category__name", "name")}
        missing = sorted(f"{n}: {o}" for n, o in pairs if (n.lower(), o.lower()) not in options)
        if missing:
            problems.append("Not a tracking option in Xero (add it in Xero, then refresh): " + ", ".join(missing)
                            + ".")
    if adj.reverse_date and adj.reverse_date <= adj.date:
        problems.append("The reversal date must be after the journal date.")
    return problems


def file_narration(adj):
    """The narration in the import file: it carries the number and file revision, so iLA finds it in Xero."""
    return f"{adj.narration} [{adj.code}-p{adj.file_revision}]"


def import_rows(adj):
    rows = []
    for narration, day, sign in ((file_narration(adj), adj.date, 1),
                                 (f"Reversal: {file_narration(adj)}", adj.reverse_date, -1)):
        if day is None:
            continue
        for line in adj.lines.all():
            rows.append([narration, f"{day:%d/%m/%Y}", line.description, line.account_code,
                         line.tax_rate or DEFAULT_TAX, f"{sign * line.amount:.2f}", line.tracking_name1,
                         line.tracking_option1, line.tracking_name2, line.tracking_option2])
    return rows


def _build_file(org, user, journals):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    rows = [row for adj in journals for row in import_rows(adj)]
    writer.writerows(rows)
    content = out.getvalue().encode("utf-8")
    refs = ", ".join(f"{a.code}-p{a.file_revision}" for a in journals)
    name = (f"{journals[0].code}-p{journals[0].file_revision} {journals[0].narration}" if len(journals) == 1
            else f"Overlay journals {refs}")[:200] + ".csv"
    import_file = ImportFile.objects.create(
        organisation=org, target_system=SourceSystem.XERO, profile="Xero manual journal", file_name=name,
        row_count=len(rows), content=content, content_type="text/csv", built_by=user,
        validation={"journals": [a.code for a in journals], "refs": refs})
    import_file.stamp(user, content)
    AuditLog.objects.create(organisation=org, user=user, action=AuditLog.Action.STAMP, input=f"stamp {name}",
                            target_type="core.importfile", target_id=str(import_file.pk),
                            result={"sha256": import_file.sha256, "journals": refs})
    return import_file


@transaction.atomic
def generate(org, user, journals):
    """Generate the Xero import file for these drafts: each becomes To be posted. Raises OverlayError with every
    problem if any of them fails its checks (then nothing changes)."""
    problems = [f"{adj.code}: {text}" for adj in journals for text in checks(adj)]
    if problems:
        raise OverlayError(" ".join(problems))
    replaced = []
    for adj in journals:
        if adj.by_ila:  # taking iLA's draft as it stands makes it yours
            adj.made_by = user
        else:  # your copy of one of iLA's: iLA's leaves the bucket
            replaced += list(Adjustment.objects.filter(
                organisation=org, made_by__isnull=True, status=DRAFT, origin=adj.origin, origin_ref=adj.origin_ref,
                date=adj.date).exclude(origin=""))
        adj.status, adj.file_revision = TO_POST, adj.file_revision + 1
    import_file = _build_file(org, user, journals)
    for adj in journals:
        adj.import_file = import_file
        adj.save()
    for old in replaced:
        leave(old, user, f"replaced by {next(a.code for a in journals if a.origin_ref == old.origin_ref)}")
    return import_file


@transaction.atomic
def back_to_draft(adj, user):
    """To be posted → Draft. Its file is cancelled; other journals in the same file get a new one."""
    old = adj.import_file
    others = list(Adjustment.objects.filter(import_file=old).exclude(pk=adj.pk).prefetch_related("lines")) \
        if old else []
    adj.status, adj.import_file = DRAFT, None
    adj.save()
    if old is not None:
        old.status, old.rejected_reason = ImportFile.Status.REJECTED, f"{adj.code} was taken back to draft"
        old.save()
    new_file = None
    if others:
        for other in others:
            other.file_revision += 1
        new_file = _build_file(adj.organisation, user, others)
        for other in others:
            other.import_file = new_file
            other.save()
    AuditLog.objects.create(organisation=adj.organisation, user=user, action=AuditLog.Action.OVERRIDE,
                            input=f"{adj.code} back to draft", target_type="core.adjustment", target_id=str(adj.pk))
    return new_file


def leave(adj, user, why):
    """Take a journal out of the bucket for good; the audit log keeps what it was."""
    AuditLog.objects.create(
        organisation=adj.organisation, user=user, action=AuditLog.Action.OVERRIDE,
        input=f"{adj.reference} left the bucket: {why}"[:2000], target_type="core.adjustment",
        target_id=str(adj.pk), result={"journal": adj.reference, "why": why, "status": adj.status,
                                       "made_by": str(adj.made_by) if adj.made_by_id else "iLA",
                                       **snapshot(adj)})
    adj.delete()


def settle(org, user=None):
    """Look for To be posted journals in iLA's copy of Xero (by the reference in the narration). Found with the
    current file: it leaves the bucket. Found with an older, cancelled file: a warning.
    Returns (references that left, warnings)."""
    left, warnings = [], []
    for adj in Adjustment.objects.filter(organisation=org, file_revision__gt=0).prefetch_related("lines"):
        found = list(Journal.objects.filter(organisation=org, narration__icontains=f"[{adj.code}-p",
                                            status=Journal.Status.POSTED,
                                            date__gte=adj.date - timedelta(days=62)))
        if not found:
            continue
        current = f"[{adj.code}-p{adj.file_revision}]".lower()
        if adj.status == TO_POST and any(current in j.narration.lower() for j in found):
            left.append(adj.reference)
            leave(adj, user, "found in Xero")
        else:
            warnings.append(f"{adj.code} is in Xero from an earlier file ({found[0].narration.strip()}), but it "
                            f"has changed since ({adj.reference}). Fix it in Xero, or type posted "
                            f"{adj.number} if Xero is right.")
    return left, warnings
