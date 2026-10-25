"""Reading a bill's PDF to know which month it is for.

Bills arriving after month end are the strongest evidence of what the month cost, but the bill date doesn't say what
the bill is for: an AV bill dated 1 October may be for an event on 24 September or 3 October. The PDF usually does.
Claude reads it and returns only facts the document states (the service period, the events with their dates, the
dated lines, a PO), each with the words and page it came from. Code then decides the month:

- events with dates: all held by month end → the month's; all after → not; a mix → the share of the dated lines
  held by month end;
- else a service period: ended by month end → the month's; started after → not; across it → by days;
- else dated lines, as for events;
- else nothing to go on: the usual rules (event tags, billing lag) decide.

Each attachment is read once (kept by its SHA-256 with what was read; the file itself isn't stored).
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.conf import settings

from console import llm
from .models import BillReading

logger = logging.getLogger(__name__)

READABLE = ("application/pdf", "image/png", "image/jpeg", "image/jpg")
MAX_BYTES = 10 * 1024 * 1024
SYSTEM = (
    "You read a supplier's bill for an accountant closing the month. Report only what the document itself says; "
    "never guess or infer. For every fact give the exact words from the document (quote) and the page number. "
    "Write dates as YYYY-MM-DD (the document is Australian: 03/10/2026 is 3 October 2026). Leave out anything the "
    "document doesn't state."
)
TOOL = "record_bill"
DESCRIPTION = "Record what the bill says about the period, events and dates it is for."
_FACT = {"quote": {"type": "string", "description": "The exact words from the document."},
         "page": {"type": "integer"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
        "service_period": {
            "type": "object", "description": "The period the bill says it covers (e.g. 'Usage 1-30 Sep 2026').",
            "properties": {"from": {"type": "string"}, "to": {"type": "string"}, **_FACT}},
        "events": {
            "type": "array", "description": "Events, functions or shows the bill names, with their dates.",
            "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "date": {"type": "string"}, "end_date": {"type": "string"}, **_FACT}}},
        "lines": {
            "type": "array", "description": "Lines that state a date (delivery, shift, hire day), at most 40.",
            "items": {"type": "object", "properties": {
                "description": {"type": "string"}, "date": {"type": "string"}, "amount": {"type": "number"},
                **_FACT}}},
        "purchase_order": {"type": "string"},
        "notes": {"type": "string", "description": "Anything else the bill says about which period it is for."},
    },
    "required": ["events", "lines"],
}


@dataclass
class Placement:
    """Which month a bill belongs to according to its PDF: kind is "month" (wholly the month's), "later" (not the
    month's), "spans" (``share`` of it is the month's) or "unknown"; with the reason and the words it rests on."""

    kind: str
    reason: str = ""
    quote: str = ""
    page: int | None = None
    share: Decimal | None = None
    file: str = ""


def _day(text):
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _amount(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _by_dates(items, period_end, what):
    """Placement from dated things (events or lines): [(date, amount or None, name, quote, page)]."""
    dated = [i for i in items if i[0] is not None]
    if not dated:
        return None
    before = [i for i in dated if i[0] <= period_end]
    first = dated[0]
    if len(before) == len(dated):
        last = max(dated, key=lambda i: i[0])
        return Placement("month", f"{what} {last[2]} on {last[0]:%d %b %Y}, by month end".replace("  ", " "),
                         last[3], last[4])
    if not before:
        soonest = min(dated, key=lambda i: i[0])
        return Placement("later", f"{what} {soonest[2]} on {soonest[0]:%d %b %Y}, after month end".replace("  ", " "),
                         soonest[3], soonest[4])
    amounts = [i[1] for i in dated]
    if all(a is not None for a in amounts) and sum(amounts):
        share = (sum(i[1] for i in before) / sum(amounts)).quantize(Decimal("0.0001"))
        return Placement("spans", f"{len(before)} of {len(dated)} dated {what}s by month end ({share:.0%} of the "
                                  "dated amount)", first[3], first[4], share)
    return Placement("unknown", f"{what}s both before and after month end, without amounts to split them")


def placement(facts, period_end):
    """Where the PDF puts the bill relative to ``period_end`` (see the module notes)."""
    facts = facts or {}
    events = [(_day(e.get("date")), None, e.get("name", ""), e.get("quote", ""), e.get("page"))
              for e in facts.get("events") or []]
    found = _by_dates(events, period_end, "event")
    if found:
        return found
    period = facts.get("service_period") or {}
    start, end = _day(period.get("from")), _day(period.get("to"))
    if end is not None or start is not None:
        quote, page = period.get("quote", ""), period.get("page")
        if end is not None and end <= period_end:
            return Placement("month", f"service period to {end:%d %b %Y}, by month end", quote, page)
        if start is not None and start > period_end:
            return Placement("later", f"service period from {start:%d %b %Y}, after month end", quote, page)
        if start is not None and end is not None and end > start:
            share = (Decimal((period_end - start).days + 1) / Decimal((end - start).days + 1)).quantize(Decimal("0.0001"))
            return Placement("spans", f"service period {start:%d %b} to {end:%d %b %Y}: {share:.0%} by month end",
                             quote, page, share)
    lines = [(_day(line.get("date")), _amount(line.get("amount")), line.get("description", "")[:60],
              line.get("quote", ""), line.get("page")) for line in facts.get("lines") or []]
    found = _by_dates(lines, period_end, "line")
    return found or Placement("unknown", "the PDF doesn't say which period it is for")


def placements(documents, period_end):
    """{document id: Placement} from the readings of ``documents`` (the first readable attachment that places it)."""
    found = {}
    for reading in BillReading.objects.filter(document__in=documents, error="").order_by("read_at"):
        place = placement(reading.facts, period_end)
        place.file = reading.file_name
        current = found.get(reading.document_id)
        if current is None or (current.kind == "unknown" and place.kind != "unknown"):
            found[reading.document_id] = place
    return found


def read_bill(document, source):
    """Read the bill's PDF (and image) attachments not read before. Returns the readings, old and new. A file that
    can't be read is recorded with the reason; a missing permission or model set-up is raised."""
    readings = []
    for attachment in source.bill_attachments(document.source_id):
        kind = (attachment["type"] or "").lower()
        if kind not in READABLE and not attachment["name"].lower().endswith(".pdf"):
            continue
        if attachment["size"] > MAX_BYTES:
            continue
        content, content_type = source.bill_attachment(document.source_id, attachment["id"])
        digest = hashlib.sha256(content).hexdigest()
        existing = BillReading.objects.filter(document=document, sha256=digest).first()
        if existing is not None:
            readings.append(existing)
            continue
        reading = BillReading(organisation=document.organisation, document=document, file_name=attachment["name"][:255],
                              sha256=digest, content_type=(content_type or kind)[:100], model=settings.ILA_LLM_MODEL)
        who = document.contact.name if document.contact_id else "a supplier"
        try:
            reading.facts = llm.call_tool(
                system=SYSTEM, tool_name=TOOL, description=DESCRIPTION, schema=SCHEMA,
                content=[llm.document_block(attachment["name"], content, content_type or kind),
                         {"type": "text", "text": f"Bill {document.number} from {who}, dated {document.date:%d %b %Y}. "
                                                  "Record what it says about the period, events and dates it is for."}])
        except llm.LLMError as exc:
            if not llm.available():
                raise
            reading.error = str(exc)[:500]
        reading.save()
        readings.append(reading)
    return readings


def bills_to_read(accrual, period_end, until):
    """Bills worth reading for the month: dated after month end up to ``until``, with attachments, with a line on an
    account an active (non-payroll) accrual profile covers, and not read yet."""
    from core.models import Document

    from .models import Behaviour

    accounts = list(accrual.profiles.filter(is_active=True).exclude(behaviour=Behaviour.PAYROLL)
                    .values_list("account_id", flat=True))
    return list(Document.objects.filter(organisation=accrual.organisation, type=Document.Type.BILL,
                                        status__in=(Document.Status.AUTHORISED, Document.Status.PAID),
                                        has_attachments=True, date__gt=period_end, date__lte=until,
                                        lines__account__in=accounts)
                .exclude(readings__isnull=False).exclude(source_id="").distinct().select_related("contact")
                .order_by("date", "pk"))


def read_bills(accrual, period_end, until, source, progress=lambda text: None):
    """Read every bill to read for the month. Returns (read, failed); stops on a permission or set-up problem."""
    todo = bills_to_read(accrual, period_end, until)
    read = failed = 0
    for n, document in enumerate(todo, 1):
        progress(f"Reading bill {n} of {len(todo)} ({document.number or document.pk})…")
        found = read_bill(document, source)
        read += 1
        failed += sum(1 for r in found if r.error)
    return read, failed
