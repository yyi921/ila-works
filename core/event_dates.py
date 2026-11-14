"""Suggested dates for dimension values (events) that have none, for a person to confirm.

Two sources, best first:
1. The code: TECC's event codes start with the event date, 261017TOW = 17 Oct 2026.
2. Sales invoices tracked or tagged to the value: dates written in their line descriptions, e.g.
   "Venue Hire - Arena - 09.10.26" or "Friday 17/10/2026".

With a code date, the event starts on it and runs to the last description date within EVENT_SPAN days after it.
Without one, description dates give the start and end when they lie within EVENT_SPAN days of each other; a wider
spread is left for a person (it may be a season, or a typo).
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Q

from .models import DimensionValue, Document, DocumentLine, Tag

CODE_DATE = re.compile(r"^(\d{2})(\d{2})(\d{2})")  # 261008MEL = 8 Oct 2026
TEXT_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")  # 09.10.26, 17/10/2026
EVENT_SPAN = 14  # days: dates further apart than this aren't one event
NEAR_INVOICE = 400  # days: a description date this far from its invoice date is ignored


@dataclass
class Suggestion:
    value: DimensionValue
    start: date
    end: date
    source: str


def code_date(code):
    """The date a code starts with (YYMMDD), or None."""
    m = CODE_DATE.match((code or "").strip())
    if not m:
        return None
    try:
        return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def text_dates(text, near):
    """Day-first dates written in ``text`` (dd.mm.yy, dd/mm/yyyy, dd-mm-yy), keeping those within NEAR_INVOICE
    days of ``near``."""
    found = []
    for d, m, y in TEXT_DATE.findall(text or ""):
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            day = date(year, int(m), int(d))
        except ValueError:
            continue
        if near is None or abs((day - near).days) <= NEAR_INVOICE:
            found.append(day)
    return found


def _invoice_dates(value):
    """Dates in the descriptions of sales invoice lines tracked (in the source) or tagged (in iLA) to ``value``."""
    tagged = Tag.objects.filter(dimension_value=value, document_line__isnull=False).exclude(
        status__in=Tag.NOT_LIVE).values("document_line_id")
    tracked = Q(tracking=value.tracking_option_id) if value.tracking_option_id else Q(pk__in=[])
    lines = (DocumentLine.objects.filter(organisation=value.organisation, document__type=Document.Type.INVOICE)
             .exclude(document__status__in=(Document.Status.VOIDED, Document.Status.DELETED))
             .filter(tracked | Q(pk__in=tagged)).select_related("document").distinct())
    dates = []
    for line in lines:
        dates += text_dates(line.description, line.document.date)
    return sorted(set(dates))


def suggest(value):
    """A Suggestion for one value without dates, or None when nothing reliable is found."""
    start = code_date(value.code)
    written = _invoice_dates(value)
    if start:
        later = [d for d in written if start <= d <= start + timedelta(days=EVENT_SPAN)]
        if later and max(later) > start:
            return Suggestion(value, start, max(later), "Code, and dates on its sales invoices")
        return Suggestion(value, start, start, "Code")
    if written and (written[-1] - written[0]).days <= EVENT_SPAN:
        return Suggestion(value, written[0], written[-1], "Dates on its sales invoices")
    return None


def suggest_all(values):
    """Suggestions for the values in ``values`` that have no start date yet."""
    out = []
    for value in values:
        if value.start_date is None:
            found = suggest(value)
            if found:
                out.append(found)
    return out
