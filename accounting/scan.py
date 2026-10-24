"""Learn an organisation's accruals from its own history.

Reads every posting to the accrual account(s) over the history window (TECC: 2000, 24 months). An accrual journal
credits the accrual account and debits the cost; the cost lines say what was accrued. They are grouped into
profiles (supplier or description × cost account × location/department), and for each profile the scan decides
how the cost behaves, matches the bills that arrived after month end (billing lag, how close the accrual was),
and writes a memo (what it found) and a rule (how iLA would accrue it).

Only iLA's copy of the ledger is read; ``copy_history`` brings the window across from Xero first. The source
ledger is never changed.
"""

import calendar
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import Account, BankTransactionLine, Contact, Document, DocumentLine, Journal, JournalLine

from .models import Accrual, AccrualProfile, Behaviour

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | \
         {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}
NOISE = {"accrue", "accrued", "accrual", "accruals", "accr", "acc", "for", "of", "the", "to", "and", "month",
         "mth", "monthly", "inv", "invoice", "est", "estimate", "estimated", "reversal", "reverse", "reversing",
         "reversed", "fy", "period", "per", "on", "in", "re", "a"}
COMPANY_WORDS = {"pty", "ltd", "limited", "inc", "co", "the", "australia", "aust", "group", "holdings", "trust",
                 "corporation", "corp", "services", "service"}
DRIVER_WORDS = {"event", "events", "function", "functions", "security", "laundry", "linen", "room", "rooms",
                "covers", "casual", "casuals", "labour", "labor", "staffing", "agency", "hire"}
DRIVER_CATEGORY_WORDS = ("event", "function", "job", "project")
FIXED_CV = 0.05             # month-to-month variation below this is a fixed amount
MATCH_TOLERANCE = 0.35      # a bill within 35% of the accrual is taken as the bill for it
BILL_WINDOW_DAYS = 75       # bills dated up to this long after month end can be the bill for an accrual
DORMANT_MONTHS = 6
ZERO = Decimal("0")
CENT = Decimal("0.01")


def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def months_between(a, b):
    return (b.year - a.year) * 12 + b.month - a.month


def words(text):
    return re.findall(r"[a-z][a-z&']*", (text or "").lower())


def description_key(text):
    """What a description says once months, years, numbers and accrual words are taken out: 'Accrue Origin
    electricity Sep-26' and 'Origin Electricity accrual October 2026' both give 'origin electricity'."""
    seen, kept = set(), []
    for w in words(text):
        if w in NOISE or w in MONTHS or len(w) < 2 or w in seen:
            continue
        seen.add(w)
        kept.append(w)
    return " ".join(kept[:6])


def company_tokens(name):
    return [w for w in words(name) if w not in COMPANY_WORDS]


def money(value):
    return f"${Decimal(value).quantize(Decimal('1')):,}"


def pct(value):
    return f"{value:.0%}"


@dataclass
class Posting:
    """One cost line of a journal that touched the accrual account. Debit (positive) raises an accrual, credit
    releases one."""

    date: date
    account: Account
    amount: Decimal
    description: str
    tracking: list
    reference: str


@dataclass
class Group:
    key: str
    account: Account
    supplier: Contact | None = None
    supplier_found_by: str = ""
    label: str = ""
    location: list = field(default_factory=list)       # TrackingOptions that are part of the key
    raises: list = field(default_factory=list)
    releases: list = field(default_factory=list)
    driver_options: Counter = field(default_factory=Counter)
    driver_by_month: dict = field(default_factory=lambda: defaultdict(set))


@dataclass
class ScanResult:
    accrual: Accrual
    start: date
    end: date
    summary: dict
    profiles: list


def default_window(history_months, today=None):
    """The last ``history_months`` full months before today."""
    today = today or timezone.localdate()
    end = month_start(today) - timedelta(days=1)
    return add_months(month_start(end), 1 - history_months), end


def copy_history(accrual, source, start, end):
    """Copy bills, credit notes, bank transactions and manual journals for the window from the source system."""
    from xero.sync import copy_period

    return copy_period(accrual.organisation, source, ("bills", "credits", "bank", "journals"), start, end)


class _Scanner:
    def __init__(self, accrual, start, end):
        self.accrual, self.org, self.start, self.end = accrual, accrual.organisation, start, end
        self.accrual_accounts = list(accrual.accounts())
        self.accrual_ids = {a.pk for a in self.accrual_accounts}
        chosen = list(accrual.driver_categories.all()) if accrual.pk else []
        self.driver_category_ids = {c.pk for c in chosen}
        self.auto_driver = not chosen
        self.suppliers = self._suppliers()
        self.summary = Counter()

    # --- Reading the ledger ---

    def _suppliers(self):
        """[(name tokens, Contact)], longest first, for finding a supplier named in a description."""
        found = []
        for c in Contact.objects.filter(organisation=self.org):
            tokens = company_tokens(c.name)
            if tokens and len("".join(tokens)) >= 4:
                found.append((tokens, c))
        return sorted(found, key=lambda t: -len(" ".join(t[0])))

    def _named_supplier(self, text):
        """The supplier whose whole name is in the text, else the one whose distinctive first word is (Origin
        for Origin Energy), when no other supplier's name starts with that word."""
        have = set(words(text))
        for tokens, contact in self.suppliers:
            if all(t in have for t in tokens):
                return contact
        firsts = Counter(tokens[0] for tokens, _ in self.suppliers)
        for tokens, contact in self.suppliers:
            if len(tokens[0]) >= 5 and firsts[tokens[0]] == 1 and tokens[0] in have:
                return contact
        return None

    def _is_driver(self, option):
        if self.auto_driver:
            return any(w in option.category.name.lower() for w in DRIVER_CATEGORY_WORDS)
        return option.category_id in self.driver_category_ids

    def postings(self):
        journals = (Journal.objects.filter(organisation=self.org, status=Journal.Status.POSTED,
                                           date__range=(self.start, end_of_bills(self.end)),
                                           lines__account__in=list(self.accrual_ids))
                    .distinct().prefetch_related("lines__account", "lines__tracking__category"))
        for journal in journals:
            self.summary["journals"] += 1
            for line in journal.lines.all():
                if line.account_id in self.accrual_ids:
                    self.summary["accrual_lines"] += 1
                    continue
                if line.account.account_class not in (Account.AccountClass.EXPENSE, Account.AccountClass.REVENUE):
                    self.summary["balance_sheet_lines"] += 1  # reclasses, e.g. accruals ↔ prepayments
                    continue
                yield Posting(journal.date, line.account, line.amount,
                              line.description or journal.narration, list(line.tracking.all()),
                              journal.number or f"journal {journal.date:%d %b %Y}")

    def group(self):
        groups = {}
        for p in self.postings():
            raised = p.amount > 0
            if raised and p.date > self.end:
                continue  # read past the window only to see releases
            supplier = self._named_supplier(p.description)
            location = sorted((o for o in p.tracking if not self._is_driver(o)), key=lambda o: o.pk)
            drivers = [o for o in p.tracking if self._is_driver(o)]
            what = f"s{supplier.pk}" if supplier else f"d:{description_key(p.description)}"
            key = f"{p.account.code or p.account.pk}|{what}|{','.join(str(o.pk) for o in location)}"
            g = groups.get(key)
            if g is None:
                g = groups[key] = Group(key, p.account, supplier, "description" if supplier else "",
                                        description_key(p.description), location)
            if raised:
                g.raises.append(p)
                for o in drivers:
                    g.driver_options[o.name] += 1
                    g.driver_by_month[month_start(p.date)].add(o.pk)
            else:
                g.releases.append(p)
        return [g for g in groups.values() if g.raises]

    # --- Bills that arrived after month end ---

    def bills(self, account, supplier):
        """Bill lines on ``account`` (or coded straight to the accrual account) dated in the window and up to
        BILL_WINDOW_DAYS after it: [(date, contact, document, net amount)]."""
        lines = (DocumentLine.objects
                 .filter(organisation=self.org, document__type=Document.Type.BILL,
                         document__status__in=(Document.Status.AUTHORISED, Document.Status.PAID),
                         document__date__range=(self.start, end_of_bills(self.end)))
                 .filter(account__in=[account.pk, *self.accrual_ids])
                 .select_related("document__contact"))
        if supplier is not None:
            lines = lines.filter(document__contact=supplier)
        docs = {}
        for line in lines:
            doc = line.document
            net = line.line_amount - (line.tax_amount if doc.line_amount_type == "inclusive" else ZERO)
            row = docs.setdefault(doc.pk, [doc.date, doc.contact, doc, ZERO])
            row[3] += net
        return list(docs.values())

    def match_bills(self, g, monthly):
        """For each accrued month, the bill that most likely settled it. Finds the supplier from the bills when
        the description didn't name one."""
        candidates = self.bills(g.account, g.supplier)
        matches = {}
        for month, amount in monthly.items():
            close = month_end(month)
            best = None
            for bill_date, contact, doc, net in candidates:
                if not (close < bill_date <= close + timedelta(days=BILL_WINDOW_DAYS)) or net <= 0:
                    continue
                gap = abs(net - amount) / amount
                if gap <= MATCH_TOLERANCE and (best is None or gap < best[0]):
                    best = (gap, bill_date, contact, doc, net)
            if best:
                matches[month] = best
        if g.supplier is None and matches:
            counts = Counter(m[2].pk for m in matches.values() if m[2] is not None)
            if counts:
                pk, n = counts.most_common(1)[0]
                if n >= max(2, len(monthly) // 2):
                    g.supplier = Contact.objects.get(pk=pk)
                    g.supplier_found_by = "bills"
            matches = {m: v for m, v in matches.items() if g.supplier and v[2] and v[2].pk == g.supplier.pk}
        return matches

    # --- One profile ---

    def profile(self, g):
        monthly = defaultdict(Decimal)
        for p in g.raises:
            monthly[month_start(p.date)] += p.amount
        monthly = dict(sorted(monthly.items()))
        months = list(monthly)
        amounts = [float(a) for a in monthly.values()]
        n, first, last = len(months), months[0], months[-1]
        span = months_between(first, last) + 1
        gaps = [months_between(a, b) for a, b in zip(months, months[1:])]
        mean = statistics.fmean(amounts)
        cv = statistics.pstdev(amounts) / mean if n > 1 and mean else 0.0
        trailing = statistics.fmean(amounts[-3:])
        stats = {"mean": round(mean, 2), "median": round(statistics.median(amounts), 2), "min": round(min(amounts), 2),
                 "max": round(max(amounts), 2), "variation": round(cv, 3), "trailing_3": round(trailing, 2),
                 "coverage": round(n / span, 2), "median_gap_months": statistics.median(gaps) if gaps else None,
                 "by_month": {m.strftime("%Y-%m"): float(a) for m, a in monthly.items()}}

        matches = self.match_bills(g, monthly)
        lags = [(m[1] - month_end(month)).days for month, m in matches.items()]
        errors = [float(m[0]) for m in matches.values()]
        released = self._released(g, monthly)
        cleared = sum(1 for m in matches.values() if any(l.account_id in self.accrual_ids
                                                        for l in m[3].lines.all()))

        behaviour, driver = self.behaviour(g, n, span, gaps, cv)
        if behaviour == Behaviour.VARIABLE and n >= 12:
            stats["seasonality"] = seasonality(monthly)
        if behaviour == Behaviour.DRIVER and g.driver_by_month:
            counts = [len(g.driver_by_month.get(m, ())) for m in months]
            per = [a / c for a, c in zip(amounts, counts) if c]
            if per:
                stats["driver_rate"] = round(statistics.fmean(per), 2)
                stats["driver_count"] = len(g.driver_options)

        dormant = months_between(last, month_start(self.end)) >= DORMANT_MONTHS
        lag = statistics.median(lags) if lags else None
        lag_std = statistics.pstdev(lags) if len(lags) > 1 else None
        mape = statistics.fmean(errors) if errors else None
        confidence = self.confidence(behaviour, n, span, gaps, mape, driver)
        name = self.name(g)
        facts = Facts(g, name, behaviour, driver, monthly, stats, n, span, first, last, lag, lag_std, len(lags),
                      mape, released, cleared, dormant)
        rule = make_rule(facts, self.accrual)
        return {
            "key": g.key[:255], "name": name[:255], "detected_behaviour": behaviour, "supplier": g.supplier,
            "supplier_found_by": g.supplier_found_by, "account": g.account, "location": g.location,
            "driver": driver, "months_accrued": n, "first_month": first, "last_month": last,
            "average": Decimal(str(mean)).quantize(CENT), "last_amount": monthly[last].quantize(CENT),
            "amount_model": stats, "billing_lag_days": lag, "lag_std": lag_std, "accuracy_mape": mape,
            "confidence": confidence, "is_current": not dormant, "memo": memo(facts), "rule": rule,
            "rule_text": rule_text(rule, facts),
            "evidence": [{"date": p.date.isoformat(), "reference": p.reference, "amount": float(p.amount),
                          "description": p.description[:200]} for p in sorted(g.raises + g.releases,
                                                                              key=lambda p: p.date)][-60:],
        }

    def _released(self, g, monthly):
        """Accrued months released in the next month: {month: day of month of the release}."""
        totals, days = defaultdict(Decimal), {}
        for p in g.releases:
            prior = add_months(month_start(p.date), -1)
            totals[prior] -= p.amount
            days[prior] = min(days.get(prior, 31), p.date.day)
        return {m: days[m] for m, total in totals.items()
                if m in monthly and abs(total - monthly[m]) <= max(monthly[m] * Decimal("0.02"), Decimal("1"))}

    def behaviour(self, g, n, span, gaps, cv):
        driver = ""
        if sum(g.driver_options.values()) >= len(g.raises) / 2 and len(g.driver_options) >= 3:
            driver = next(iter(driver_noun(o.category.name) for p in g.raises for o in p.tracking
                               if self._is_driver(o)), "")
        elif n >= 3 and (DRIVER_WORDS & set(g.label.split())):
            driver = "to be confirmed"
        if driver and n >= 3:
            return Behaviour.DRIVER, driver
        if n <= 2:
            return Behaviour.ONE_OFF, ""
        median_gap = statistics.median(gaps)
        if median_gap >= 2 and all(abs(x - median_gap) <= 1 for x in gaps):
            return Behaviour.PERIODIC, ""
        if median_gap == 1 and n / span >= 0.6:
            return (Behaviour.FIXED if cv <= FIXED_CV else Behaviour.VARIABLE), ""
        return Behaviour.ONE_OFF, ""

    @staticmethod
    def confidence(behaviour, n, span, gaps, mape, driver):
        """0–1: more months, steadier timing and closer to the bill earn more."""
        score = min(n, 12) / 12
        if behaviour in (Behaviour.FIXED, Behaviour.VARIABLE):
            score *= n / span
        elif behaviour == Behaviour.PERIODIC:
            score *= 1.0 if len(set(gaps)) == 1 else 0.8
        if mape is not None:
            score *= 1 - min(mape, 0.5)
        if behaviour == Behaviour.ONE_OFF:
            score = min(score, 0.3)
        if behaviour == Behaviour.DRIVER and driver == "to be confirmed":
            score = min(score, 0.5)
        return round(score, 2)

    @staticmethod
    def name(g):
        what = g.supplier.name if g.supplier else (g.label.title() if g.label else g.account.name)
        where = ", ".join(o.name for o in g.location)
        return f"{what} — {g.account.code} {g.account.name}" + (f" ({where})" if where else "")


def driver_noun(category_name):
    """What one driver option is: 'Event' for an Events Department category."""
    name = category_name.lower()
    return next((w.title() for w in DRIVER_CATEGORY_WORDS if w in name), category_name)


def end_of_bills(end):
    return end + timedelta(days=BILL_WINDOW_DAYS)


def seasonality(monthly):
    """Calendar month → index against the overall average (1.2 = 20% above), for months seen at least once."""
    overall = statistics.fmean(float(a) for a in monthly.values())
    by_month = defaultdict(list)
    for m, a in monthly.items():
        by_month[m.month].append(float(a))
    return {calendar.month_abbr[k]: round(statistics.fmean(v) / overall, 2) for k, v in sorted(by_month.items())}


@dataclass
class Facts:
    group: Group
    name: str
    behaviour: str
    driver: str
    monthly: dict
    stats: dict
    n: int
    span: int
    first: date
    last: date
    lag: float | None
    lag_std: float | None
    bills_matched: int
    mape: float | None
    released: dict
    cleared: int
    dormant: bool


def memo(f):
    """What the history shows, in plain words."""
    g, s = f.group, f.stats
    where = ", ".join(o.name for o in g.location)
    to = f"{g.account.code} {g.account.name}" + (f" ({where})" if where else "")
    if f.n == 1:
        lines = [f"Accrued once, in {f.first:%b %Y}, to {to}."]
    elif f.behaviour == Behaviour.PERIODIC:
        lines = [f"Accrued to {to} {f.n} times between {f.first:%b %Y} and {f.last:%b %Y}."]
    else:
        lines = [f"Accrued to {to} in {f.n} of the {f.span} months from {f.first:%b %Y} to {f.last:%b %Y}."]
    if f.behaviour == Behaviour.FIXED:
        lines.append(f"The same amount each time: {money(s['median'])} (varied by under {pct(FIXED_CV)}).")
    elif f.behaviour == Behaviour.VARIABLE:
        lines.append(f"Average {money(s['mean'])}, from "
                     f"{money(s['min'])} to {money(s['max'])} (varies {pct(s['variation'])} month to month); "
                     f"last three months average {money(s['trailing_3'])}.")
        season = s.get("seasonality") or {}
        high = [m for m, v in season.items() if v >= 1.15]
        low = [m for m, v in season.items() if v <= 0.85]
        if high or low:
            lines.append("Seasonal: " + "; ".join(filter(None, [
                f"higher in {', '.join(high)}" if high else "", f"lower in {', '.join(low)}" if low else ""])) + ".")
    elif f.behaviour == Behaviour.DRIVER:
        if "driver_rate" in s:
            noun = f.driver.lower()
            lines.append(f"Follows {noun} activity: {s['driver_count']} different {noun}s tagged, about "
                         f"{money(s['driver_rate'])} per {noun}.")
        else:
            lines.append(f"The description suggests it follows activity (\"{g.label}\"); the driver needs "
                         f"confirming. Average {money(s['mean'])}.")
    elif f.behaviour == Behaviour.PERIODIC:
        gap = s["median_gap_months"]
        lines.append(f"Accrued about every {gap:g} months, typically {money(s['median'])}; next expected "
                     f"{add_months(f.last, int(gap)):%b %Y}. A periodic cost belongs in prepayments or "
                     "amortisation rather than a monthly accrual.")
    else:
        when = ", ".join(f"{money(a)} in {m:%b %Y}" for m, a in list(f.monthly.items())[-4:])
        lines.append(f"No regular pattern: {when}. Accrue only when there is evidence.")

    if f.bills_matched:
        who = g.supplier.name if g.supplier else "the supplier"
        lines.append(f"The bill from {who} is usually dated {f.lag:g} days after month end"
                     + (f" (±{f.lag_std:.0f})" if f.lag_std is not None else "")
                     + f", found for {f.bills_matched} of {f.n} months; accruals were within {pct(f.mape)} "
                       "of the bill on average.")
        if g.supplier_found_by == "bills":
            lines.append("Supplier worked out from the bills that followed, not named in the journals.")
    elif f.behaviour != Behaviour.ONE_OFF:
        lines.append("No bill within 35% of the accrual was found after month end: check how this cost is "
                     "billed (consolidated bill, different account, or paid by direct debit).")
    if f.released:
        days = Counter(f.released.values()).most_common(1)[0][0]
        lines.append(f"Reversed on day {days} of the next month in {len(f.released)} of {f.n} months.")
    elif f.cleared:
        lines.append(f"Cleared by bills coded straight to the accrual account ({f.cleared} times).")
    else:
        lines.append("No reversal found the following month: check it was released.")
    if f.dormant and f.behaviour != Behaviour.ONE_OFF:
        lines.append(f"Not accrued since {f.last:%b %Y}: it may have stopped.")
    return " ".join(lines)


def make_rule(f, accrual):
    g, s = f.group, f.stats
    base = {"account": g.account.code, "tracking": [o.name for o in g.location],
            "supplier": g.supplier.name if g.supplier else None, "reverse": "first day of next month",
            "skip_if_bill_posted": True, "materiality": float(accrual.materiality)}
    if f.lag is not None:
        base["expected_bill_lag_days"] = f.lag
    if f.behaviour == Behaviour.FIXED:
        return base | {"method": "fixed", "amount": s["median"]}
    if f.behaviour == Behaviour.VARIABLE:
        return base | {"method": "run_rate", "basis": "trailing 3-month average", "amount": s["trailing_3"],
                       "seasonality": s.get("seasonality")}
    if f.behaviour == Behaviour.DRIVER:
        return base | {"method": "driver_rate", "driver": f.driver, "rate": s.get("driver_rate"),
                       "fallback": "trailing 3-month average", "fallback_amount": s["trailing_3"]}
    if f.behaviour == Behaviour.PERIODIC:
        return {"method": "prepayment", "account": g.account.code, "cadence_months": s["median_gap_months"],
                "typical_amount": s["median"], "next_expected": add_months(f.last, int(s["median_gap_months"]))
                .strftime("%Y-%m"), "supplier": base["supplier"]}
    return {"method": "evidence_only", "account": g.account.code, "sources": ["cut-off scan", "PO", "user input"],
            "supplier": base["supplier"]}


def rule_text(rule, f):
    method = rule["method"]
    tail = ""
    if "expected_bill_lag_days" in rule:
        tail = (f" Skip it if the bill is already posted; it usually arrives {rule['expected_bill_lag_days']:g} days "
                "after month end.")
    if method not in ("evidence_only", "prepayment") and f.stats["mean"] < rule["materiality"]:
        tail += f" Below the {money(rule['materiality'])} materiality floor, so not proposed."
    if f.dormant and method not in ("evidence_only", "prepayment"):
        tail += " Paused: not accrued for six months; confirm it still applies."
    dest = f"Dr {rule['account']}" + (f" ({', '.join(rule['tracking'])})" if rule.get("tracking") else "")
    if method == "fixed":
        return f"Accrue {money(rule['amount'])} every month, {dest} / Cr accruals, reversed on day 1.{tail}"
    if method == "run_rate":
        season = " × the month's seasonal index" if rule.get("seasonality") else ""
        return (f"Accrue the last three months' average ({money(rule['amount'])}){season}, {dest} / Cr accruals, "
                f"reversed on day 1.{tail}")
    if method == "driver_rate":
        if rule.get("rate"):
            return (f"Accrue {money(rule['rate'])} per {rule['driver'].lower()} held in the month, {dest} / Cr "
                    f"accruals, reversed on day 1.{tail}")
        return (f"Accrue driver volume × rate once the driver is confirmed; until then the last three months' "
                f"average ({money(rule['fallback_amount'])}), {dest}.{tail}")
    if method == "prepayment":
        return (f"Don't accrue monthly. Every {rule['cadence_months']:g} months (next {rule['next_expected']}), "
                f"spread about {money(rule['typical_amount'])} over the period it covers: prepay if billed in "
                "advance, accrue monthly portions if billed in arrears.")
    return "Only accrue when there is evidence: a bill found in the cut-off scan, a PO receipt, or a user's note."


@transaction.atomic
def scan(accrual, start=None, end=None):
    """Learn profiles from the accrual account's history and save them. Reviewer choices (behaviour changed,
    notes, switched off) are kept. Returns a ScanResult."""
    if start is None or end is None:
        start, end = default_window(accrual.history_months)
    scanner = _Scanner(accrual, start, end)
    if not scanner.accrual_accounts:
        raise ValueError(f"No account with code {accrual.account_codes} in {accrual.organisation}'s chart of "
                         "accounts. Refresh the chart from Xero or fix the accrual account code.")
    found = [scanner.profile(g) for g in scanner.group()]
    existing = {p.key: p for p in accrual.profiles.all()}
    saved = []
    for data in found:
        location = data.pop("location")
        row = existing.pop(data["key"], None) or AccrualProfile(organisation=accrual.organisation, accrual=accrual)
        keep = row.pk is not None and row.behaviour != row.detected_behaviour
        for k, v in data.items():
            setattr(row, k, v)
        if not keep:
            row.behaviour = row.detected_behaviour
        row.save()
        row.tracking.set(location)
        saved.append(row)
    for row in existing.values():  # no longer in the history
        if row.is_reviewed:
            row.is_current = False
            row.save(update_fields=["is_current", "updated_at"])
        else:
            row.delete()

    by_behaviour = Counter(p.behaviour for p in saved)
    summary = dict(scanner.summary) | {
        "profiles": len(saved), "by_behaviour": {b.label: by_behaviour.get(b.value, 0) for b in Behaviour},
        "accrued_total": round(sum(sum(p.amount_model["by_month"].values()) for p in saved), 2),
        "accounts": [str(a) for a in scanner.accrual_accounts]}
    accrual.last_scanned_at, accrual.scanned_from, accrual.scanned_to = timezone.now(), start, end
    accrual.scan_summary = summary
    accrual.save()
    return ScanResult(accrual, start, end, summary, saved)
