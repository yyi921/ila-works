"""Labour by day and by event, worked out from the pay lines, the event dates and the changes people made.

1. **Days.** Each pay line is spread evenly over its days (Leave Start → Leave End). Super doesn't have days of its
   own: it follows the employee's wages in the pay run, day by day.
2. **Events.** Each employee-day on a cost account that can go to an event is allocated by the first rule that
   applies:

   | # | Rule | Reason shown |
   |---|------|--------------|
   | 1 | A person set this employee's days | Changed by you |
   | 2 | A person set the whole day | Day set by you |
   | 3 | Event day of one or more events (start → end) | Event day (split equally if several) |
   | 4 | Bump day (± bump days, set on the dimension or the event) | Bump-in −1 / bump-out +2 |
   | 5 | A season is running (e.g. 2627FIRE) | Season |
   | 6 | Nothing | No event → Unassigned |

   Cost accounts that can't go to an event are always Unassigned ("Not taggable").
3. **On-costs** (payroll tax, workcover) are a rate of pay, so they follow the pay they're on: same day, same
   department, same events.

Nothing here is stored: it is worked out again whenever the pay lines, event dates or changes move.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from core.models import DimensionValue

from .ledger import runs_touching, settings_for
from .models import LabourOverride, PayLine

ZERO, ONE = Decimal("0"), Decimal("1")
SUPER = "super"


@dataclass
class Piece:
    """One employee's pay on one day, from one pay line, and where it goes."""

    run_id: int
    employee_id: int
    day: object
    cost_account: object
    component: object
    amount: Decimal
    hours: Decimal
    shares: list = field(default_factory=list)  # [(value id or None for Unassigned, share)]
    rule: str = ""
    reason: str = ""

    @property
    def is_super(self):
        return (self.component.transaction_type or "").strip().lower() == SUPER


@dataclass
class Month:
    """Everything the Labour view shows for one period."""

    start: object
    end: object
    pieces: list
    values: dict  # value id -> DimensionValue
    on_cost_rate: Decimal
    rates_missing: list
    calendar: dict  # day -> [(DimensionValue, kind, offset)]
    overrides: list

    def on_cost(self, amount):
        return amount * self.on_cost_rate


def _days(first, last):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def spread(lines):
    """[Piece] for ``lines`` (of whole pay runs), before allocation. Super follows the employee's wages."""
    pieces, supers = [], []
    wages = defaultdict(lambda: defaultdict(lambda: ZERO))  # (run, employee) -> day -> wages
    for line in lines:
        if (line.transaction_type or "").strip().lower() == SUPER:
            supers.append(line)
            continue
        days = list(_days(line.date_from, line.date_to))
        amount, hours = line.amount / len(days), line.hours / len(days)
        for day in days:
            pieces.append(Piece(line.run_id, line.employee_id, day, line.cost_account, line.pay_component, amount,
                                hours))
            wages[(line.run_id, line.employee_id)][day] += amount
    for line in supers:
        days = list(_days(line.date_from, line.date_to))
        weights = {d: w for d, w in wages[(line.run_id, line.employee_id)].items() if d in set(days) and w > 0}
        if not weights:
            weights = {d: ONE for d in days}
        total = sum(weights.values())
        for day, weight in weights.items():
            pieces.append(Piece(line.run_id, line.employee_id, day, line.cost_account, line.pay_component,
                                line.amount * weight / total, ZERO))
    return pieces


def calendar(dimension, first, last):
    """{day: [(value, kind, offset)]} for ``first``..``last``: kind is "event", "bump" or "season"; offset is the
    days before the start (negative) or after the end (positive) for a bump day."""
    found = defaultdict(list)
    if dimension is None:
        return found
    values = (DimensionValue.objects.filter(dimension=dimension, is_active=True, start_date__isnull=False)
              .select_related("dimension"))
    for value in values:
        bump_from, start, end, bump_to = value.window()
        if bump_to < first or bump_from > last:
            continue
        season = value.is_season
        for day in _days(max(bump_from, first), min(bump_to, last)):
            if start <= day <= end:
                found[day].append((value, "season" if season else "event", 0))
            elif not season:
                found[day].append((value, "bump", (day - start).days if day < start else (day - end).days))
    return found


def _equal(values):
    share = ONE / len(values)
    return [(v.pk, share) for v in values]


def _codes(values):
    return ", ".join(v.code for v in values)


def allocate(piece, on_day, employee_changes, day_changes):
    """Fill in ``piece.shares``, ``rule`` and ``reason``."""
    if not piece.cost_account.taggable:
        piece.shares, piece.rule = [(None, ONE)], "not_taggable"
        piece.reason = f"Not taggable ({piece.cost_account.name or piece.cost_account.code})"
        return
    for changes, rule, words in ((employee_changes.get(piece.employee_id, ()), "employee", "Changed by you"),
                                 (day_changes, "day", "Day set by you")):
        change = next((c for c in changes if c.date_from <= piece.day <= c.date_to), None)
        if change is not None:
            piece.shares, piece.rule = [(change.value_id, ONE)], rule
            piece.reason = words + (f": {change.note}" if change.note else "")
            return
    entries = on_day.get(piece.day, [])
    for kind, rule in (("event", "event"), ("bump", "bump"), ("season", "season")):
        found = [(v, offset) for v, k, offset in entries if k == kind]
        if not found:
            continue
        values = [v for v, _ in found]
        piece.shares, piece.rule = _equal(values), rule
        if kind == "event":
            piece.reason = f"Event day: {_codes(values)}"
        elif kind == "season":
            piece.reason = f"Season: {_codes(values)}"
        else:
            piece.reason = "; ".join(f"Bump-{'in ' if offset < 0 else 'out +'}{offset}: {v.code}" for v, offset in found)
        if len(values) > 1:
            piece.reason += " (split equally)"
        return
    piece.shares, piece.rule, piece.reason = [(None, ONE)], "none", "No event"


def month(org, start, end):
    """The Labour view's data for ``start``..``end``: the pay of every pay run touching the period, on the days in
    it, allocated to events."""
    return allocate_runs(org, list(runs_touching(org, start, end)), start, end)


def allocate_runs(org, runs, start, end):
    """The pay of ``runs`` on the days ``start``..``end``, allocated to events."""
    settings = settings_for(org)
    lines = (PayLine.objects.filter(run__in=runs).select_related("cost_account", "pay_component")
             .order_by("run", "employee", "date_from", "pk"))
    pieces = [p for p in spread(lines) if start <= p.day <= end]

    dimension = settings.event_dimension
    on_day = calendar(dimension, start, end)
    overrides = list(LabourOverride.objects.filter(organisation=org, date_from__lte=end, date_to__gte=start)
                     .select_related("employee", "value", "set_by").order_by("-created_at", "-pk"))
    employee_changes, day_changes = defaultdict(list), []
    for change in overrides:
        if change.employee_id:
            employee_changes[change.employee_id].append(change)
        else:
            day_changes.append(change)
    for piece in pieces:
        allocate(piece, on_day, employee_changes, day_changes)

    ids = {v for p in pieces for v, _ in p.shares if v}
    values = {v.pk: v for v in DimensionValue.objects.filter(pk__in=ids)}
    for entries in on_day.values():
        values.update({v.pk: v for v, _, _ in entries})
    costs = list(settings.on_costs.all())
    rate = sum((c.rate for c in costs if c.rate is not None), ZERO)
    return Month(start, end, pieces, values, rate, [c.name for c in costs if c.rate is None], on_day, overrides)


# --- Totals for the views ---------------------------------------------------------------------------------------

def _cost(m, piece, with_on_costs):
    return piece.amount + (m.on_cost(piece.amount) if with_on_costs else ZERO)


def by_employee(m, with_on_costs=True):
    """{employee id: {value id or None: amount}}."""
    out = defaultdict(lambda: defaultdict(lambda: ZERO))
    for p in m.pieces:
        cost = _cost(m, p, with_on_costs)
        for value, share in p.shares:
            out[p.employee_id][value] += cost * share
    return out


def by_day(m, with_on_costs=True):
    """{day: {"values": {value id: amount}, "unassigned": amount, "not_taggable": amount, "total": amount}}."""
    out = defaultdict(lambda: {"values": defaultdict(lambda: ZERO), "unassigned": ZERO, "not_taggable": ZERO,
                               "total": ZERO, "hours": ZERO})
    for p in m.pieces:
        cost, row = _cost(m, p, with_on_costs), out[p.day]
        row["total"] += cost
        row["hours"] += p.hours
        if p.rule == "not_taggable":
            row["not_taggable"] += cost
            continue
        for value, share in p.shares:
            if value:
                row["values"][value] += cost * share
            else:
                row["unassigned"] += cost * share
    return out


def by_event(m):
    """{value id or None: {"employees": set, "hours", "wages", "super", "on_costs", "total"}}; None is Unassigned,
    split into "not_taggable" and "no_event" amounts too."""
    out = defaultdict(lambda: {"employees": set(), "hours": ZERO, "wages": ZERO, "super": ZERO, "on_costs": ZERO,
                               "total": ZERO, "not_taggable": ZERO, "no_event": ZERO})
    for p in m.pieces:
        on_cost = m.on_cost(p.amount)
        for value, share in p.shares:
            row = out[value]
            row["employees"].add(p.employee_id)
            row["hours"] += p.hours * share
            row["super" if p.is_super else "wages"] += p.amount * share
            row["on_costs"] += on_cost * share
            row["total"] += (p.amount + on_cost) * share
            if value is None:
                row["not_taggable" if p.rule == "not_taggable" else "no_event"] += (p.amount + on_cost) * share
    return out


def employee_days(m, employee_id, with_on_costs=True):
    """[(day, cost account, components, hours, amount, shares, reason)] for one employee, one row per day and cost
    account."""
    rows = {}
    for p in m.pieces:
        if p.employee_id != employee_id:
            continue
        key = (p.day, p.cost_account.pk)
        row = rows.setdefault(key, {"day": p.day, "cost_account": p.cost_account, "components": set(),
                                    "hours": ZERO, "amount": ZERO, "shares": defaultdict(lambda: ZERO),
                                    "reasons": set()})
        cost = _cost(m, p, with_on_costs)
        row["components"].add(p.component.code)
        row["hours"] += p.hours
        row["amount"] += cost
        for value, share in p.shares:
            row["shares"][value] += cost * share
        row["reasons"].add(p.reason)
    return [rows[k] for k in sorted(rows, key=lambda k: (k[0], rows[k]["cost_account"].code))]
