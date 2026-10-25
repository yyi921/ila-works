"""An accrual profile's rule: what it says in words, and the amount it gives for a period."""

import calendar
from decimal import Decimal

from core.models import DocumentLine, Event

import statistics

from .models import Behaviour, Method
from .util import CENT, money


def _dest(p):
    if not p.account_id:
        return ""
    where = ", ".join(o.name for o in p.tracking.all()) if p.pk else ""
    return f"Dr {p.account.code}" + (f" ({where})" if where else "") + " / Cr accruals"


def describe(p):
    """The rule in words, from the profile's current fields (so it always matches what a run will do)."""
    if not p.is_active:
        return "Switched off: a run leaves it out."
    reverse = ", reversed on day 1" if p.reverse_next_month else ", not reversed"
    tail = ""
    if p.skip_if_bill_posted and p.billing_lag_days is not None:
        tail = f" Skip it if the bill is already posted; it usually arrives {p.billing_lag_days:g} days after month end."
    elif p.skip_if_bill_posted:
        tail = " Skip it if the bill is already posted."
    first = "Accrue the month's bill if it's in by the close, else "
    if p.method == Method.FIXED:
        if p.amount is None:
            return "Fixed amount, but no amount is set: a run leaves it out until one is."
        return f"{first}{money(p.amount)} every month, {_dest(p)}{reverse}.{tail}"
    if p.method == Method.RUN_RATE:
        if p.amount is None:
            return "Run-rate, but no base amount is set: a run leaves it out until one is."
        season = " × the month's seasonal index" if p.seasonality else ""
        base = (f"{money(p.amount)}" if "amount" in p.kept_fields
                else f"the average of the {p.accrual.trailing_months} months before (now {money(p.amount)})")
        return f"{first}{base}{season}, {_dest(p)}{reverse}.{tail}"
    if p.method == Method.DRIVER_RATE:
        if p.driver and p.driver_rate is not None:
            fallback = f"; if no {p.driver.lower()}s are found, {money(p.amount)}" if p.amount is not None else ""
            return (f"Accrue {money(p.driver_rate)} per {p.driver.lower()} in the month{fallback}, {_dest(p)}"
                    f"{reverse}.{tail}")
        if p.amount is not None:
            return f"No driver or rate set: accrue the run-rate ({money(p.amount)}), {_dest(p)}{reverse}.{tail}"
        return "Driver-linked, but no driver, rate or amount is set: a run leaves it out."
    if p.method == Method.PREPAYMENT:
        gap = (p.amount_model or {}).get("median_gap_months")
        every = f" Every {gap:g} months" if gap else ""
        typical = f", spread about {money(p.amount)}" if p.amount is not None else ", spread the bill"
        return (f"Don't accrue monthly.{every}{typical} over the period it covers: prepay if billed in advance, "
                "accrue monthly portions if billed in arrears.")
    if p.method == Method.EVIDENCE_ONLY:
        return "Only accrue when there is evidence: a bill after month end for this month, a PO receipt, or a note."
    return "Don't accrue."


def driver_volume(p, start, end):
    """How many driver units (e.g. events) were active in the period, and where that came from."""
    if p.driver_category_id:
        options = (DocumentLine.objects.filter(organisation=p.organisation, document__date__range=(start, end),
                                               document__type="invoice",
                                               tracking__category_id=p.driver_category_id)
                   .values_list("tracking", flat=True).distinct())
        n = len(set(options))
        if n:
            return n, f"{n} {p.driver.lower() or 'driver'}s on sales invoices"
    events = Event.objects.filter(organisation=p.organisation, start_date__lte=end, end_date__gte=start).count()
    if events:
        return events, f"{events} events in the calendar"
    return 0, ""


def prior_months(p, start):
    """[(YYYY-MM, amount)] accrued before ``start``: the history a run may learn from, never the month itself."""
    by_month = (p.amount_model or {}).get("by_month") or {}
    cut = start.strftime("%Y-%m")
    return [(m, Decimal(str(a))) for m, a in sorted(by_month.items()) if m < cut]


def _season(p, before, end):
    """The month's seasonal index: as a person set it, else learned from a year or more of months before the period
    (so the period's own accrual can't move it), else none."""
    if "seasonality" in p.kept_fields or not p.seasonality:
        return Decimal(str((p.seasonality or {}).get(calendar.month_abbr[end.month], 1)))
    if len(before) < 12:
        return Decimal("1")
    overall = sum((a for _, a in before), Decimal("0")) / len(before)
    same = [a for m, a in before if int(m[5:]) == end.month]
    if not same or not overall:
        return Decimal("1")
    return (sum(same, Decimal("0")) / len(same) / overall).quantize(Decimal("0.01"))


def _label(months):
    first, last = months[0][0], months[-1][0]
    fmt = lambda m: f"{calendar.month_abbr[int(m[5:])]} {m[:4]}"
    return fmt(first) if first == last else f"{fmt(first)} to {fmt(last)}"


def expected(p, start, end):
    """(amount, basis) for the period when no bill is in yet, or (None, why not). Learned only from months before
    the period, unless a person set the amount."""
    before = prior_months(p, start)
    if p.behaviour == Behaviour.PAYROLL and p.method == Method.NONE:
        return None, "payroll, worked out separately (not built yet)"
    if p.method == Method.FIXED:
        if "amount" not in p.kept_fields and before:
            recent = before[-12:]
            amount = Decimal(str(statistics.median(a for _, a in recent))).quantize(CENT)
            return amount, f"the usual amount ({_label(recent)})"
        return (p.amount, "fixed amount") if p.amount is not None else (None, "no fixed amount set")
    if p.method == Method.RUN_RATE:
        index = _season(p, before, end)
        season = f" × {index} for {calendar.month_abbr[end.month]}" if index != 1 else ""
        if "amount" not in p.kept_fields and before:
            recent = before[-p.accrual.trailing_months:]
            base = sum((a for _, a in recent), Decimal("0")) / len(recent)
            return (base * index).quantize(CENT), f"average of {_label(recent)}{season}"
        if p.amount is None:
            return None, "no run-rate amount set"
        return (p.amount * index).quantize(CENT), "run-rate" + season
    if p.method == Method.DRIVER_RATE:
        if p.driver and p.driver_rate is not None:
            volume, found = driver_volume(p, start, end)
            if volume:
                return (p.driver_rate * volume).quantize(CENT), f"{found} × {money(p.driver_rate)}"
        if p.amount is not None:
            return p.amount, "run-rate (no driver volume found)"
        return None, "no driver volume or amount"
    if p.method == Method.PREPAYMENT:
        return None, "prepayment, not accrued"
    if p.method == Method.EVIDENCE_ONLY:
        return None, "only from evidence"
    return None, "set to don't accrue"
