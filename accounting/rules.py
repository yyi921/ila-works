"""An accrual profile's rule: what it says in words, and the amount it gives for a period."""

import calendar
from decimal import Decimal

from core.models import DocumentLine, Event

from .models import Method
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
    if p.method == Method.FIXED:
        if p.amount is None:
            return "Fixed amount, but no amount is set: a run leaves it out until one is."
        return f"Accrue {money(p.amount)} every month, {_dest(p)}{reverse}.{tail}"
    if p.method == Method.RUN_RATE:
        if p.amount is None:
            return "Run-rate, but no base amount is set: a run leaves it out until one is."
        season = " × the month's seasonal index" if p.seasonality else ""
        return f"Accrue the run-rate ({money(p.amount)}){season}, {_dest(p)}{reverse}.{tail}"
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


def expected(p, start, end):
    """(amount, basis) for the period, or (None, why not)."""
    if p.method == Method.FIXED:
        return (p.amount, "fixed amount") if p.amount is not None else (None, "no fixed amount set")
    if p.method == Method.RUN_RATE:
        if p.amount is None:
            return None, "no run-rate amount set"
        index = Decimal(str((p.seasonality or {}).get(calendar.month_abbr[end.month], 1)))
        basis = "run-rate" + (f" × {index} for {calendar.month_abbr[end.month]}" if index != 1 else "")
        return (p.amount * index).quantize(CENT), basis
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
