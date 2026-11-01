"""The open-item ledger of a balance sheet account: its descriptions and the register of postings tagged to them.

Opening + movements = closing, and closing = the descriptions still open. The ledger starts from a seed: the
accountant's breakdown of the opening balance (a seed file), or one iLA works out by reading a few months before the
opening date. Whatever the seed doesn't explain is the "Opening not analysed" description, which later releases that
match nothing newer run down.

Every record touching the account (a manual journal, bill, credit note, spend or receive money; or a row of an
uploaded GL) is read whole, the way an accountant reads it:

- **Raising** (moving the balance away from nil: a credit to an accrual account, a debit to a prepayment): one
  description per category among the lines on the other side of the record (a payroll journal is one Labour
  description, not thirty lines). In a standing category it first uses up open descriptions of the other sign.
- **Releasing** (a reversal, amortisation, or a bill coded to the account) finds what it releases, strongest first:
  the mirror of an earlier record (the same other-side accounts with the opposite amounts); the record with the same
  narration and total; the descriptions naming its invoice; one open description of exactly that amount; the same
  narration; an earlier record with the same total; the same category, oldest first; the opening not analysed.
  What is left becomes its own description, flagged.

Each posting says how it was linked; anything not certain is "Proposed" for a person to confirm or move. A person's
decisions (a posting moved or confirmed; a description's text, category, counterparty, reference or note changed)
are kept and re-applied when the ledger is rebuilt. The amounts are always the ledger's; nothing changes in Xero.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction

from core.models import Account, BankTransaction, Document, Journal

from .models import CategoryBehaviour, OpenItem, OpenItemCategory, OpenItemDecision, OpenItemLedger, OpenItemPosting
from .scan import invoice_refs
from .util import month_end

ZERO = Decimal("0")
CENT = Decimal("0.01")
REVERSAL = re.compile(r"^\s*revers(?:al|ed|ing|e)?\b\s*(?:of\b)?\s*(?:accruals?\s+for\b)?\s*:?\s*", re.I)
POSTED_DOCS = (Document.Status.AUTHORISED, Document.Status.PAID)
Movement, Confidence, Origin = OpenItemPosting.Movement, OpenItemPosting.Confidence, OpenItem.Origin
UNASSIGNED = "Not assigned yet"


def norm(text):
    """Narration without "Reversal:" and punctuation, for comparing."""
    return re.sub(r"[^a-z0-9]+", " ", REVERSAL.sub("", text or "").lower()).strip()


def is_reversal(text):
    return bool(re.match(r"^\s*revers", text or "", re.I))


def lines_text(numbers):
    """[1, 2, 3, 5] -> "1-3, 5"."""
    numbers, runs = sorted({n for n in numbers if n}), []
    for n in numbers:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def natural_sign(account):
    """-1 when the account's balance is normally a credit (liabilities, equity), +1 for assets."""
    return 1 if account.account_class == Account.AccountClass.ASSET else -1


# --- Records: what moved the account ------------------------------------------------------------------------------

@dataclass
class Line:
    number: int
    amount: Decimal                    # debit positive
    description: str = ""
    account: str = ""                  # for other-side lines: the account code
    account_name: str = ""


@dataclass
class Record:
    kind: str                          # Manual journal, Bill, Supplier credit note, Spend money, GL row ...
    record_id: str
    number: str
    date: date
    narration: str
    contact: str = ""
    mine: list = field(default_factory=list)     # lines on the account
    others: list = field(default_factory=list)   # the other side

    @property
    def total(self):
        return sum((l.amount for l in self.mine), ZERO)

    @property
    def txn(self):
        abbr = {"Manual journal": "MJ", "Bill": "BILL", "Supplier credit note": "SCN", "Sales invoice": "INV",
                "Customer credit note": "CCN", "Spend money": "SPEND", "Receive money": "RECV"}.get(self.kind, "GL")
        return f"{abbr}-{self.record_id[:8]}-L{lines_text(l.number for l in self.mine)}"

    @property
    def label(self):
        return f"{self.kind} {self.number}" if self.number else f"{self.kind} {self.date:%d %b %Y}"

    @property
    def text(self):
        return self.narration or "; ".join(dict.fromkeys(l.description for l in self.mine if l.description))

    def other_map(self):
        found = defaultdict(Decimal)
        for line in self.others:
            found[line.account] += line.amount
        return {k: v for k, v in found.items() if v}


def records_from_copy(account, start, end):
    """Records touching ``account`` dated ``start`` to ``end`` in iLA's copy of Xero."""
    org, records = account.organisation, []
    for j in (Journal.objects.filter(organisation=org, status=Journal.Status.POSTED, date__range=(start, end),
                                     lines__account=account).distinct().prefetch_related("lines__account")):
        lines = list(j.lines.all())
        records.append(Record(
            "Manual journal", j.source_id or f"ila{j.pk}", j.number, j.date, (j.narration or "").strip(), "",
            [Line(l.line_number, l.amount, l.description) for l in lines if l.account_id == account.pk],
            [Line(l.line_number, l.amount, l.description, l.account.code, l.account.name) for l in lines
             if l.account_id != account.pk]))
    for doc in (Document.objects.filter(organisation=org, status__in=POSTED_DOCS, date__range=(start, end),
                                        lines__account=account).distinct()
                .select_related("contact").prefetch_related("lines__account")):
        payable = doc.ledger == Document.Ledger.PAYABLE
        credit = doc.type == Document.Type.CREDIT_NOTE
        kind = {(True, False): "Bill", (True, True): "Supplier credit note", (False, False): "Sales invoice",
                (False, True): "Customer credit note"}[(payable, credit)]
        sign = Decimal(1) if payable != credit else Decimal(-1)  # a bill debits what it's coded to
        mine = []
        for l in doc.lines.all():
            if l.account_id != account.pk:
                continue
            net = l.line_amount - (l.tax_amount if doc.line_amount_type == "inclusive" else ZERO)
            mine.append(Line(l.line_number, sign * net, l.description))
        records.append(Record(kind, doc.source_id or f"ila{doc.pk}", doc.number, doc.date,
                              (doc.reference or "").strip(), doc.contact.name if doc.contact else "", mine,
                              []))
    for bt in (BankTransaction.objects.filter(organisation=org, status=BankTransaction.Status.AUTHORISED,
                                              date__range=(start, end), lines__account=account).distinct()
               .select_related("contact").prefetch_related("lines")):
        spend = bt.direction == BankTransaction.Direction.SPEND
        sign = Decimal(1) if spend else Decimal(-1)
        mine = []
        for l in bt.lines.all():
            if l.account_id != account.pk:
                continue
            net = l.line_amount - (l.tax_amount if bt.line_amount_type == "inclusive" else ZERO)
            mine.append(Line(l.line_number, sign * net, l.description))
        records.append(Record("Spend money" if spend else "Receive money", bt.source_id or f"ila{bt.pk}",
                              bt.reference, bt.date, (bt.reference or "").strip(),
                              bt.contact.name if bt.contact else "", mine, []))
    records.sort(key=lambda r: (r.date, 0 if r.kind == "Manual journal" else 1, r.record_id))
    return [r for r in records if r.mine]


def records_from_gl(rows, start, end):
    """Records from an uploaded GL (see gl.read_gl): one row is one record, without its other side."""
    records = []
    for row in rows:
        day = date.fromisoformat(row["date"])
        if not start <= day <= end:
            continue
        text = row.get("description", "")
        records.append(Record(row.get("source") or "GL row", row["id"], row.get("reference", ""), day, text,
                              row.get("contact", ""), [Line(1, Decimal(row["amount"]), text)], []))
    records.sort(key=lambda r: (r.date, r.record_id))
    return records


# --- Categories -------------------------------------------------------------------------------------------------

WORD = re.compile(r"[a-z][a-z&]{2,}")
STOP = {"and", "the", "for", "expense", "expenses", "other", "costs", "cost", "general", "review", "accrual",
        "accruals", "accrue", "charges", "based", "invoice", "invoices", "fees"}
# Words too common in accrual and prepayment narrations to say which item a release belongs to.
COMMON = STOP | {"prepaid", "prepayment", "prepayments", "amortise", "amortised", "amortisation", "amortization",
                 "reversal", "reverse", "monthly", "month", "fee", "from", "with", "this", "that", "per", "inv",
                 "ltd", "pty", "australia"}


def significant(text):
    """Distinctive words (4+ letters) for telling which item a narration is about."""
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in COMMON}


def fraction_of(part, whole):
    """n if ``part`` is about 1/n of ``whole`` (n from 2 to 36, within rounding), else 0."""
    part, whole = abs(part), abs(whole)
    if not part or whole <= part:
        return 0
    n = int((whole / part).to_integral_value())
    return n if 2 <= n <= 36 and abs(whole - part * n) <= Decimal("0.01") * n + Decimal("0.05") else 0


class Categoriser:
    """Which category a line belongs to: a word the category lists, else its account, else a word shared between
    the line's account name and the category's name or scope (a suggestion). None = not assigned yet."""

    def __init__(self, categories):
        self.categories = [c for c in categories if c.is_active]
        self.by_account = {code: c for c in self.categories for code in c.account_codes()}
        self.learned = {}  # account -> category, learned from how the seed matched Xero's journals

    @staticmethod
    def _terms(text):
        return {w for w in WORD.findall((text or "").lower()) if w not in STOP}

    def __call__(self, text, account="", account_name=""):
        low = (text or "").lower()
        for c in self.categories:
            for w in c.word_list():
                if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low):
                    return c
        if account in self.by_account:
            return self.by_account[account]
        if account in self.learned:
            return self.learned[account]
        if account_name:
            mine = self._terms(account_name)
            best, score = None, 0
            for c in self.categories:
                shared = len(mine & self._terms(f"{c.name} {c.scope}"))
                if shared > score:
                    best, score = c, shared
            return best
        return None


# --- The build ----------------------------------------------------------------------------------------------------

@dataclass
class Item:
    origin_key: str
    category: object
    description: str
    origin: str
    raised_on: date
    code: str = ""
    counterparty: str = ""
    reference: str = ""
    service_period: date = None
    refs: list = field(default_factory=list)
    match_key: str = ""
    match_accounts: dict = field(default_factory=dict)
    raised_by: str = ""
    original_amount: Decimal = ZERO
    balance: Decimal = ZERO
    released: bool = False
    bill: dict = field(default_factory=dict)
    parts: list = field(default_factory=list)  # for a seed line: the earlier Xero records that make it up
    release_keys: set = field(default_factory=set)  # narrations of the releases linked to it so far
    release_proposed: bool = False  # whether any of those links was only proposed


@dataclass
class Post:
    item: Item
    txn: str
    date: date
    movement: str
    amount: Decimal
    record: Record = None
    basis: str = ""
    confidence: str = Confidence.LINKED
    from_lines: str = ""


class Builder:
    """Works out the ledger's descriptions and postings in memory; ``save`` writes them."""

    def __init__(self, ledger, categories, existing=None, pins=None, prefix="D"):
        self.ledger = ledger
        self.sign = natural_sign(ledger.account)
        self.categorise = Categoriser(categories)
        self.existing = existing or {}           # origin_key -> OpenItem (kept codes and a person's edits)
        self.pins = pins or {}                   # txn -> item code
        self.items, self.posts = [], []
        self.unanalysed = None
        self.prefix = prefix
        self._txns = set()

    # -- items --

    def new_item(self, origin_key, category, description, origin, day, **extra):
        saved = self.existing.get(origin_key)
        if saved is not None and "category" in saved.edited:
            category = saved.category
        item = Item(origin_key, category, description, origin, day, **extra)
        if saved is not None:
            item.code = saved.code
        self.items.append(item)
        return item

    def by_code(self, code):
        return next((i for i in self.items if i.code and i.code.lower() == code.lower()), None)

    def open_items(self, sign=None):
        found = [i for i in self.items if abs(i.balance) >= CENT and i is not self.unanalysed]
        if sign is not None:
            found = [i for i in found if (i.balance > 0) == (sign > 0)]
        return found

    def post(self, item, txn, day, movement, amount, record=None, basis="", confidence=Confidence.LINKED,
             from_lines=""):
        base, n = txn, 1
        while txn in self._txns:
            n += 1
            txn = f"{base}/{n}"
        self._txns.add(txn)
        pinned = self.pins.get(txn)
        if pinned and (target := self.by_code(pinned)) is not None and target is not item:
            item, basis, confidence = target, "Moved here by a person", Confidence.CONFIRMED
        elif pinned:
            confidence = Confidence.CONFIRMED
        item.balance += amount
        if movement in (Movement.REVERSAL, Movement.PAYMENT) or amount * self.sign < 0:
            item.released = item.released or movement != Movement.OPENING
        self.posts.append(Post(item, txn, day, movement, amount, record, basis, confidence, from_lines))

    # -- the seed --

    def seed(self, day, rows, opening_balance, origin=Origin.SEED):
        """``rows``: [{code, category (object or None), description, amount (debit positive), counterparty,
        reference, service_period, origin_key}]."""
        for row in rows:
            item = self.new_item(row.get("origin_key") or f"SEED:{row['code'] or row['description']}",
                                 row.get("category"), row["description"], origin,
                                 row.get("raised_on") or row.get("service_period") or day,
                                 counterparty=row.get("counterparty", ""), reference=row.get("reference", ""),
                                 service_period=row.get("service_period"),
                                 refs=row.get("refs") or ([row["reference"]] if row.get("reference") else []),
                                 match_key=row.get("match_key", ""), match_accounts=row.get("match_accounts") or {},
                                 raised_by=row.get("raised_by", "Seed"),
                                 original_amount=Decimal(row["amount"]))
            if row.get("code") and not item.code:
                item.code = row["code"]
            self.post(item, f"OPEN-{item.code or len(self.items)}", day, Movement.OPENING, Decimal(row["amount"]),
                      basis="Opening breakdown" if origin == Origin.SEED else "Worked out by iLA from the look-back",
                      confidence=Confidence.GIVEN if origin == Origin.SEED else Confidence.PROPOSED)
        rest = opening_balance - sum((Decimal(r["amount"]) for r in rows), ZERO)
        if abs(rest) >= CENT:
            self.unanalysed = self.new_item("OPEN:REST", None, f"Opening not analysed at {day:%d %b %Y}",
                                            Origin.UNANALYSED, day)
            self.post(self.unanalysed, "OPEN-REST", day, Movement.OPENING, rest,
                      basis="Xero's opening balance less the breakdown", confidence=Confidence.GIVEN)

    def explain_seed(self, candidates):
        """Match each seed line to the Xero records still open at the opening date that make it up (one, or two or
        three adding up to it), so later reversals and bills find it, and learn which accounts belong to which of
        the accountant's categories. ``candidates``: Items from a look-back pass (see look_back)."""
        free = [c for c in candidates if c.origin == Origin.RAISED and c.balance * self.sign > 0]
        seeds = sorted((i for i in self.items if i.origin == Origin.SEED), key=lambda i: -abs(i.balance))
        for item in seeds:
            target = item.balance
            pool = sorted(free, key=lambda c: (c.raised_on != item.service_period, -c.raised_on.toordinal()))
            found = next(([c] for c in pool if c.balance == target), None)
            if found is None:
                small = pool[:40]
                found = next(([a, b] for x, a in enumerate(small) for b in small[x + 1:]
                              if a.balance + b.balance == target), None)
            if found is None:
                small = pool[:25]
                found = next(([a, b, c] for x, a in enumerate(small) for y, b in enumerate(small[x + 1:], x + 1)
                              for c in small[y + 1:] if a.balance + b.balance + c.balance == target), None)
            if not found:
                continue
            for part in found:
                free.remove(part)
                item.parts.append({"record": part.origin_key.rsplit(":", 1)[0], "raised_by": part.raised_by,
                                   "key": part.match_key, "accounts": part.match_accounts, "amount": part.balance,
                                   "refs": part.refs, "used": False})
                item.refs = list(dict.fromkeys(item.refs + part.refs))
                if item.category is not None:
                    for code in part.match_accounts:
                        self.categorise.learned.setdefault(code, item.category)
            if len(found) == 1:
                item.match_key, item.match_accounts = found[0].match_key, found[0].match_accounts
            if not item.reference and item.refs:
                item.reference = ", ".join(item.refs)[:255]

    # -- records --

    def process(self, record):
        total = record.total
        if abs(total) < CENT:
            return
        pinned = self.pins.get(record.txn)
        if pinned and self.by_code(pinned) is not None:
            self.post(self.by_code(pinned), record.txn, record.date, self._movement(record, total), total, record)
            return
        if total * self.sign > 0:
            self._raise(record)
        else:
            self._release(record, total)

    def _movement(self, record, total):
        if total * self.sign > 0:
            return Movement.ADDITION
        return Movement.REVERSAL if record.kind in ("Manual journal", "GL row") else Movement.PAYMENT

    def _groups(self, record):
        """[(category, amount, text, my lines, other lines)]: the record's effect on the account by category."""
        others = record.others
        if len(record.mine) == 1 and others and sum((o.amount for o in others), ZERO) == -record.mine[0].amount:
            grouped = {}
            for o in others:
                cat = self.categorise(o.description or record.narration, o.account, o.account_name)
                g = grouped.setdefault(cat.pk if cat else None, [cat, ZERO, [], [], []])
                g[1] -= o.amount
                g[2].append(o.description or o.account_name)
                g[4].append(o)
            if len(grouped) > 1 and None in grouped:  # lines outside the structure join the main description
                loose = grouped.pop(None)
                main = max(grouped.values(), key=lambda g: abs(g[1]))
                main[1] += loose[1]
                main[2] += loose[2]
                main[4] += loose[4]
            for g in grouped.values():
                g[3] = record.mine
        else:
            other_cats = {self.categorise(o.description or record.narration, o.account, o.account_name)
                          for o in others}
            grouped = {}
            for line in record.mine:
                cat = (next(iter(other_cats)) if len(other_cats) == 1
                       else self.categorise(f"{line.description} {record.narration} {record.contact}"))
                g = grouped.setdefault((cat.pk if cat else None, line.description), [cat, ZERO, [], [], []])
                g[1] += line.amount
                g[2].append(line.description)
                g[3].append(line)
                g[4] = list(others)
        out = []
        base = REVERSAL.sub("", record.narration).strip() if is_reversal(record.narration) else record.narration
        for cat, amount, texts, mine, oth in grouped.values():
            uniq = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
            detail = "; ".join(uniq[:3]) + (f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else "")
            text = base if not detail or detail.lower() in base.lower() else (f"{base} — {detail}" if base else detail)
            out.append((cat, amount, text or record.kind, mine, oth))
        return out

    def _raise(self, record):
        refs = invoice_refs(" ".join([record.narration, record.number] + [l.description for l in record.mine]))
        if record.kind in ("Bill", "Supplier credit note", "Sales invoice", "Customer credit note") and record.number:
            refs = [record.number] + [r for r in refs if r != record.number]
        groups = self._groups(record)
        for cat, amount, text, mine, oth in groups:
            from_lines = ", ".join(f"{o.number} ({o.account})" for o in oth)
            own = refs
            if len(groups) > 1:  # several descriptions from one record: each keeps the invoices its own lines name
                own_text = [l.description for l in mine] if len(record.mine) > 1 else [o.description for o in oth]
                own = invoice_refs(" ".join(own_text)) or refs
            if cat is not None and cat.standing:
                for item in self.open_items(-self.sign):
                    if item.category is not None and item.category.pk == cat.pk and abs(amount) >= CENT:
                        take = max(amount, -item.balance) if amount < 0 else min(amount, -item.balance)
                        self.post(item, record.txn, record.date, Movement.ADDITION, take, record,
                                  "Standing category: uses up the open description of the other sign",
                                  Confidence.LINKED, from_lines)
                        amount -= take
            if abs(amount) < CENT:
                continue
            first_line = min((l.number for l in mine), default=0)
            first_other = min((o.number for o in oth), default=0)
            accounts = defaultdict(Decimal)
            for o in oth:
                accounts[o.account] += o.amount
            item = self.new_item(f"{record.record_id}:{first_line}.{first_other}", cat, text[:255], Origin.RAISED,
                                 record.date, counterparty=record.contact, reference=", ".join(own)[:255],
                                 service_period=month_end(record.date), refs=own, match_key=norm(record.narration),
                                 match_accounts={k: str(v) for k, v in accounts.items()},
                                 raised_by=record.label,
                                 original_amount=amount)
            self.post(item, record.txn, record.date, Movement.ADDITION, amount, record, "New description",
                      Confidence.NEW, from_lines)

    def _release(self, record, total):
        """Release open descriptions of the account's natural sign for ``total`` (the other sign)."""
        movement = self._movement(record, total)
        key = norm(record.narration)
        others = record.other_map()
        refs = invoice_refs(" ".join([record.narration, record.number] + [l.description for l in record.mine]))
        if record.number:
            refs = refs + [record.number]
        everything = [i for i in self.items if abs(i.balance) >= CENT and i is not self.unanalysed]
        natural = [i for i in everything if i.balance * self.sign > 0]
        by_record = defaultdict(list)  # the open descriptions each earlier record raised
        for i in everything:
            by_record[i.origin_key.rsplit(":", 1)[0] if i.origin == Origin.RAISED else i.origin_key].append(i)
        left = total

        def lines_for(item):
            wanted = set(item.match_accounts)
            chosen = [o for o in record.others if o.account in wanted] or record.others
            return ", ".join(f"{o.number} ({o.account})" for o in chosen)

        def take(items, basis, confidence, whole=False):
            nonlocal left
            for item in items:
                amount = -item.balance if whole else (
                    min(left, -item.balance) if left > 0 else max(left, -item.balance))
                if abs(amount) < CENT or (not whole and amount * left <= 0):
                    continue
                self.post(item, record.txn, record.date, movement, amount, record, basis, confidence,
                          lines_for(item))
                if key:
                    item.release_keys.add(key)
                    item.release_proposed = item.release_proposed or confidence == Confidence.PROPOSED
                left -= amount
                if abs(left) < CENT and not whole:
                    break

        def next_period(item):
            return month_end(item.raised_on) < record.date <= month_end(month_end(item.raised_on) + timedelta(days=1))

        # 0. Parts of seed lines: the earlier journals each seed line was matched to (see explain_seed).
        parts = defaultdict(list)
        for item in natural:
            for part in item.parts:
                if not part["used"]:
                    parts[part["record"]].append((item, part))
        for pairs in parts.values():
            accounts = defaultdict(Decimal)
            for _, part in pairs:
                for code, amt in part["accounts"].items():
                    accounts[code] += Decimal(amt)
            amount = sum((part["amount"] for _, part in pairs), ZERO)
            mirror = bool(others) and {k: -v for k, v in accounts.items() if v} == others
            same = bool(key) and all(part["key"] == key for _, part in pairs)
            if amount == -total and (mirror or same):
                how = "same accounts, opposite amounts" if mirror else "same narration and total"
                for item, part in pairs:
                    part["used"] = True
                    wanted = set(part["accounts"])
                    lines = [o for o in record.others if o.account in wanted] or record.others
                    self.post(item, record.txn, record.date, movement, -part["amount"], record,
                              f"Reverses {part['raised_by']}, part of seed line {item.code} ({how})",
                              Confidence.LINKED, ", ".join(f"{o.number} ({o.account})" for o in lines))
                return
        # 1. The mirror of an earlier record: the same other-side accounts with the opposite amounts.
        if others:
            for group in by_record.values():
                theirs = defaultdict(Decimal)
                for i in group:
                    for code, amt in i.match_accounts.items():
                        theirs[code] += Decimal(amt)
                if theirs and {k: -v for k, v in theirs.items() if v} == others \
                        and sum((-i.balance for i in group), ZERO) == total:
                    when = " in the next period" if all(next_period(i) for i in group) else ""
                    take(group, f"Mirror of {group[0].raised_by}: same accounts, opposite amounts{when}",
                         Confidence.LINKED, whole=True)
                    return
        # 2. The record with the same narration and total.
        for group in by_record.values():
            if key and all(i.match_key == key for i in group) and sum((-i.balance for i in group), ZERO) == total:
                take(group, f"Reverses {group[0].raised_by}: same narration and total", Confidence.LINKED,
                     whole=True)
                return
        # 3. The descriptions naming its invoice.
        low_refs = [r.lower() for r in refs if r]
        named = [i for i in natural if any(
            r and any(x.startswith(r.lower()) or r.lower().startswith(x) for x in low_refs if len(x) >= 4)
            for r in i.refs)]
        if named:
            standing = {i.category.pk for i in named if i.category is not None and i.category.standing}
            if standing:  # a periodic bill settles every open description in its standing category
                named = [i for i in natural if i.category is not None and i.category.pk in standing]
            take(named, "Names the same invoice" + (" (standing category, oldest first)" if standing else ""),
                 Confidence.LINKED)
            if abs(left) < CENT:
                return
        # 4. One open description of exactly the amount.
        exact = [i for i in natural if i.balance == -left]
        if exact:
            best = next((i for i in exact if i.match_key == key and key), None) or next(
                (i for i in exact if others and set(i.match_accounts) == set(others)), None) or next(
                (i for i in exact if next_period(i)), None)
            if best is not None:
                take([best], "Same amount as one open description, and the same narration, accounts or next period",
                     Confidence.LINKED)
            else:
                take(exact[:1], "Same amount as one open description", Confidence.PROPOSED)
            return
        # 5. The same narration (part of the amount).
        if key:
            same = [i for i in natural if i.match_key == key]
            if same:
                take(same, "Same narration, oldest first", Confidence.PROPOSED)
                if abs(left) < CENT:
                    return
        # 5b. The same narration as an earlier release of an open description (the next month's amortisation).
        if key:
            again = [i for i in natural if key in i.release_keys]
            if again:
                take(again, "Same narration as its earlier releases",
                     Confidence.PROPOSED if any(i.release_proposed for i in again) else Confidence.LINKED)
                if abs(left) < CENT:
                    return
        # 6. An earlier record whose open descriptions add up to the same total.
        for group in by_record.values():
            if len(group) > 1 and sum((-i.balance for i in group), ZERO) == left:
                take(group, f"Same total as {group[0].raised_by}", Confidence.PROPOSED, whole=True)
                return
        # 6b. A description it shares distinctive words with, released in regular parts (1/12 of a year's licence).
        words = significant(" ".join([record.narration, record.contact] + [l.description for l in record.mine]))
        scored = []
        for item in natural:
            if abs(item.balance) + CENT < abs(left):
                continue
            shared = sorted(words & significant(f"{item.description} {item.counterparty} {item.reference}"))
            n = fraction_of(left, item.original_amount)
            score = 2 * len(shared) + (2 if n else 0)
            if score >= 2:
                scored.append((score, item, shared, n))
        if scored and max(s[0] for s in scored) == 2 and sum(1 for s in scored if s[3]) != 1:
            scored = []  # a fraction alone, but more than one description fits it
        if scored:
            score, item, shared, n = max(scored, key=lambda s: (s[0], -s[1].raised_on.toordinal()))
            reasons = ([f"shares {', '.join(shared)}"] if shared else []) + (
                [f"1/{n} of its original amount"] if n else [])
            take([item], "Release of " + item.description[:60] + ": " + " and ".join(reasons),
                 Confidence.LINKED if score >= 5 else Confidence.PROPOSED)
            if abs(left) < CENT:
                return
        # 7. The same category, oldest first.
        cats = {g[0].pk for g in self._groups(record) if g[0] is not None}
        same_cat = [i for i in natural if i.category is not None and i.category.pk in cats]
        if same_cat:
            rule = any(i.category.standing or i.category.behaviour == CategoryBehaviour.SPREAD for i in same_cat)
            take(same_cat, "Same category, oldest first", Confidence.LINKED if rule else Confidence.PROPOSED)
            if abs(left) < CENT:
                return
        # 8. The opening balance nobody analysed.
        if self.unanalysed is not None and self.unanalysed.balance * self.sign > 0:
            amount = max(left, -self.unanalysed.balance) if left < 0 else min(left, -self.unanalysed.balance)
            if abs(amount) >= CENT:
                self.post(self.unanalysed, record.txn, record.date, movement, amount, record,
                          "Matches nothing newer: released from the opening not analysed", Confidence.PROPOSED,
                          ", ".join(f"{o.number} ({o.account})" for o in record.others))
                left -= amount
        if abs(left) >= CENT:
            groups = self._groups(record)
            cat = groups[0][0] if groups else None
            item = self.new_item(f"{record.record_id}:U{min((l.number for l in record.mine), default=0)}", cat,
                                 f"Matched nothing: {record.text}"[:255], Origin.UNLINKED, record.date,
                                 counterparty=record.contact, reference=", ".join(refs)[:255], refs=refs,
                                 match_key=key, raised_by=record.label,
                                 original_amount=left)
            self.post(item, record.txn, record.date, movement, left, record, "Matched no open description",
                      Confidence.NEW, ", ".join(f"{o.number} ({o.account})" for o in record.others))

    # -- writing it --

    def save(self, ledger):
        """Write the descriptions and postings, keeping codes and a person's edits. Returns (items, postings)."""
        org = ledger.organisation
        width = 4
        used = {i.code for i in self.items if i.code}
        taken = set(OpenItem.objects.filter(ledger=ledger).values_list("code", flat=True)) | used
        numbers = [int(m.group(1)) for c in taken if (m := re.fullmatch(rf"{re.escape(self.prefix)}-(\d+)", c))]
        counter = max(numbers + [0]) + 1
        for item in self.items:
            if not item.code:
                while f"{self.prefix}-{counter:0{width}d}" in taken:
                    counter += 1
                item.code = f"{self.prefix}-{counter:0{width}d}"
                taken.add(item.code)
                counter += 1
        with transaction.atomic():
            OpenItemPosting.objects.filter(ledger=ledger).delete()
            saved = {i.origin_key: i for i in OpenItem.objects.filter(ledger=ledger)}
            rows = {}
            for item in self.items:
                row = saved.get(item.origin_key) or OpenItem(organisation=org, ledger=ledger,
                                                             origin_key=item.origin_key)
                edited = set(row.edited or [])
                row.code = row.code or item.code
                values = {"category": item.category, "description": item.description[:255],
                          "counterparty": item.counterparty[:255], "reference": item.reference[:255],
                          "service_period": item.service_period}
                for name, value in values.items():
                    if name not in edited:
                        setattr(row, name, value)
                row.origin, row.raised_on, row.original_amount = item.origin, item.raised_on, item.original_amount
                row.raised_by, row.refs, row.match_key = item.raised_by[:255], item.refs, item.match_key[:255]
                row.match_accounts, row.bill, row.is_active = item.match_accounts, item.bill, True
                row.save()
                rows[id(item)] = row
            keep = {r.pk for r in rows.values()}
            for row in saved.values():
                if row.pk not in keep:
                    if row.edited:
                        row.is_active = False
                        row.save(update_fields=["is_active", "updated_at"])
                    else:
                        row.delete()
            OpenItemPosting.objects.bulk_create([OpenItemPosting(
                organisation=org, ledger=ledger, item=rows[id(p.item)], txn=p.txn, seq=n, date=p.date,
                movement=p.movement, amount=p.amount.quantize(CENT),
                record=p.record.kind if p.record else ("Seed" if p.movement == Movement.OPENING else ""),
                record_id=p.record.record_id if p.record else "",
                record_number=(p.record.number or "")[:255] if p.record else "",
                lines=lines_text(l.number for l in p.record.mine) if p.record else "",
                from_lines=p.from_lines[:255], basis=p.basis[:255], confidence=p.confidence,
                narration=(p.record.text if p.record else p.item.description)[:2000])
                for n, p in enumerate(self.posts, 1)])
        return len(rows), len(self.posts)


# --- Running it -------------------------------------------------------------------------------------------------

def _pins(ledger):
    return {d.txn: d.item_code for d in OpenItemDecision.objects.filter(ledger=ledger)}


def _seed_rows(ledger, categories):
    by_code = {c.code.lower(): c for c in categories}
    rows = []
    for row in ledger.seed_rows:
        rows.append({**row, "category": by_code.get((row.get("category") or "").lower()),
                     "service_period": date.fromisoformat(row["service_period"]) if row.get("service_period") else None,
                     "origin_key": f"SEED:{row.get('code') or row['description']}"})
    return rows


def build(ledger, to, *, records=None, lookback_records=None, lookback_balance=None):
    """Rebuild ``ledger`` up to ``to`` from its seed and the records (iLA's copy of Xero, or the uploaded GL).
    For a seed worked out by iLA, ``lookback_records`` are the months before the opening date and
    ``lookback_balance`` Xero's balance at the start of them. Returns a short summary."""
    account = ledger.account
    categories = list(OpenItemCategory.objects.filter(ledger=ledger))
    existing = {i.origin_key: i for i in OpenItem.objects.filter(ledger=ledger).select_related("category")}
    pins = _pins(ledger)
    if records is None:
        records = (records_from_gl(ledger.gl_rows, ledger.start, to) if ledger.gl_rows
                   else records_from_copy(account, ledger.start, to))
    builder = Builder(ledger, categories, existing, pins, prefix=ledger.prefix)
    if ledger.seed == OpenItemLedger.Seed.UPLOAD:
        builder.seed(ledger.opening_date, _seed_rows(ledger, categories), ledger.opening_balance)
        if lookback_records is None:
            lookback_records = context_records(ledger)
        builder.explain_seed(look_back(ledger, categories, lookback_records))
    else:
        seed = work_out_seed(ledger, categories, lookback_records or [], lookback_balance or ZERO)
        builder.seed(ledger.opening_date, seed, ledger.opening_balance, origin=Origin.ILA_SEED)
    sign = natural_sign(account)
    for record in sorted(records, key=lambda r: (r.date, 0 if r.total * sign > 0 else 1)):
        builder.process(record)
    attach_bills(ledger, builder.items)
    items, postings = builder.save(ledger)
    ledger.built_to = to
    ledger.save(update_fields=["built_to", "updated_at"])
    return {"items": items, "postings": postings, "records": len(records)}


def look_back(ledger, categories, records, balance_then=ZERO):
    """The descriptions still open at the opening date when the look-back months are read from an unanalysed
    start: [Item], amounts debit positive."""
    if not records:
        return []
    first = min(r.date for r in records).replace(day=1) - timedelta(days=1)
    look = Builder(ledger, categories, prefix=ledger.prefix)
    look.seed(first, [], balance_then)
    for record in sorted(records, key=lambda r: (r.date, 0 if r.total * look.sign > 0 else 1)):
        if record.date <= ledger.opening_date:
            look.process(record)
    return [i for i in look.items if i is not look.unanalysed and abs(i.balance) >= CENT]


def context_records(ledger):
    """The look-back months before the opening date, from the uploaded GL or iLA's copy of Xero."""
    from .openitem_jobs import lookback_start

    first = lookback_start(ledger)
    if ledger.gl_rows:
        return records_from_gl(ledger.gl_rows, first, ledger.opening_date)
    return records_from_copy(ledger.account, first, ledger.opening_date)


def work_out_seed(ledger, categories, records, balance_then):
    """iLA's own breakdown of the opening balance: what is still open at the opening date after the look-back
    months. Returns seed rows (amounts debit positive)."""
    rows = []
    for item in look_back(ledger, categories, records, balance_then):
        rows.append({"code": "", "category": item.category, "description": item.description,
                     "amount": item.balance, "counterparty": item.counterparty, "reference": item.reference,
                     "service_period": item.service_period, "raised_on": item.raised_on, "refs": item.refs,
                     "match_key": item.match_key, "match_accounts": item.match_accounts,
                     "raised_by": item.raised_by, "origin_key": f"ILASEED:{item.origin_key}"})
    return rows


def attach_bills(ledger, items):
    """The supplier's bill each description names, from iLA's copy of Xero: evidence it was billed."""
    wanted = {r.strip().lower() for i in items for r in i.refs if r and len(r.strip()) >= 4}
    if not wanted:
        return
    bills = {}
    for doc in (Document.objects.filter(organisation=ledger.organisation, type=Document.Type.BILL,
                                        status__in=POSTED_DOCS).exclude(number="").select_related("contact")):
        number = doc.number.strip().lower()
        if number in wanted or any(number.startswith(w) for w in wanted):
            bills.setdefault(number, doc)
    for item in items:
        item.bill = {}
        for ref in item.refs:
            r = (ref or "").strip().lower()
            doc = bills.get(r) or next((d for n, d in bills.items() if len(r) >= 5 and n.startswith(r)), None)
            if doc is not None:
                item.bill = {"number": doc.number, "date": doc.date.isoformat() if doc.date else "",
                             "net": str(doc.subtotal), "supplier": doc.contact.name if doc.contact else ""}
                break


def suggest_categories(ledger, records):
    """Categories for a ledger nobody has structured yet: one per account on the other side of its records (or per
    contact for records without one), so the accountant has something to rename and merge."""
    found = {}
    for record in records:
        if record.others:
            for o in record.others:
                if o.account and o.account not in found:
                    found[o.account] = (o.account_name or o.account, o.account)
        elif record.contact:
            found.setdefault(f"c:{record.contact}", (record.contact, ""))
    made = []
    for n, (key, (name, code)) in enumerate(sorted(found.items()), 1):
        made.append(OpenItemCategory.objects.get_or_create(
            organisation=ledger.organisation, ledger=ledger, code=(code or f"C{n:02d}")[:12],
            defaults={"name": name[:100], "accounts": code, "words": "" if code else name.lower()[:255],
                      "order": n})[0])
    return made


# --- What the working paper shows --------------------------------------------------------------------------------

def balances_at(ledger, as_at):
    """{item pk: balance} at the end of ``as_at`` (debit positive)."""
    found = defaultdict(Decimal)
    for item_id, amount in OpenItemPosting.objects.filter(ledger=ledger, date__lte=as_at).values_list("item_id",
                                                                                                        "amount"):
        found[item_id] += amount
    return found


def open_at(ledger, as_at):
    """[(item, balance)] open at ``as_at``, oldest first."""
    bal = balances_at(ledger, as_at)
    items = OpenItem.objects.filter(ledger=ledger, pk__in=[k for k, v in bal.items() if abs(v) >= CENT])
    return sorted(((i, bal[i.pk]) for i in items.select_related("category")),
                  key=lambda p: (p[0].raised_on or date.min, p[0].code))


def roll_forward(ledger, months):
    """Rows by month end: opening, additions, reversals, payments, adjustments, closing (debit positive)."""
    rows = []
    postings = list(OpenItemPosting.objects.filter(ledger=ledger).values_list("date", "movement", "amount"))
    opening = sum((a for d, m, a in postings if d <= ledger.opening_date), ZERO)
    for end in months:
        start = end.replace(day=1)
        moves = {m: sum((a for d, mv, a in postings if start <= d <= end and mv == m and d > ledger.opening_date),
                        ZERO) for m in (Movement.ADDITION, Movement.REVERSAL, Movement.PAYMENT, Movement.ADJUSTMENT)}
        closing = opening + sum(moves.values(), ZERO)
        rows.append({"end": end, "opening": opening, **moves, "closing": closing})
        opening = closing
    return rows


def by_category(ledger, period_end):
    """Rows (category name, opening, additions, releases, closing, open count) for the month of ``period_end``."""
    start = period_end.replace(day=1)
    rows = {}
    for p in OpenItemPosting.objects.filter(ledger=ledger, date__lte=period_end).select_related("item__category"):
        cat = p.item.category
        key = (cat.order, cat.code) if cat else (10 ** 6, "")
        row = rows.setdefault(key, {"name": f"{cat.code} {cat.name}" if cat else UNASSIGNED, "opening": ZERO,
                                    "added": ZERO, "released": ZERO, "closing": ZERO, "items": set()})
        if p.date < start:
            row["opening"] += p.amount
        elif p.amount * natural_sign(ledger.account) > 0:
            row["added"] += p.amount
        else:
            row["released"] += p.amount
        row["closing"] += p.amount
    bal = balances_at(ledger, period_end)
    for p in OpenItem.objects.filter(ledger=ledger).select_related("category"):
        if abs(bal.get(p.pk, ZERO)) >= CENT:
            cat = p.category
            key = (cat.order, cat.code) if cat else (10 ** 6, "")
            rows[key]["items"].add(p.pk)
    return [dict(r, items=len(r["items"])) for _, r in sorted(rows.items())
            if any(abs(r[k]) >= CENT for k in ("opening", "added", "released", "closing"))]


def flags(ledger, item, balance, as_at, *, aged_months=3, residual=Decimal("10")):
    """Short flags for an open description at ``as_at``."""
    found = []
    sign = natural_sign(ledger.account)
    raised = item.raised_on or ledger.opening_date
    age = (as_at.year - raised.year) * 12 + as_at.month - raised.month
    if item.origin == Origin.UNANALYSED:
        return ["OPENING-NOT-ANALYSED"]
    if age >= aged_months:
        found.append(f"AGED {age}m")
    if abs(balance) < residual:
        found.append("RESIDUAL")
    if balance * sign < 0:
        found.append("WRONG-SIGN")
    if item.origin == Origin.UNLINKED:
        found.append("UNLINKED")
    if not item.refs and item.origin != Origin.UNANALYSED:
        found.append("NO-SUPPORT")
    if item.bill and item.bill.get("date") and date.fromisoformat(item.bill["date"]) <= as_at and \
            item.origin == Origin.RAISED and sign < 0:
        found.append("BILLED-NOT-RELEASED")
    if item.category is None:
        found.append("NOT-ASSIGNED")
    return found


def stable_id(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# --- The reconciliation, for the console and admin ---------------------------------------------------------------

def shown(account, amount):
    """A debit-positive amount the way the balance sheet shows the account (a liability's credit as positive)."""
    return None if amount is None else amount * natural_sign(account)


def month_ends_between(first, last):
    ends, day = [], month_end(first)
    while day <= last:
        ends.append(day)
        day = month_end(day + timedelta(days=1))
    return ends


def report(ledger, period_end):
    """The reconciliation at ``period_end``, amounts as the balance sheet shows them: the roll-forward by month
    against Xero, the month by category, the open descriptions and the totals."""
    account = ledger.account
    months = []
    for row in roll_forward(ledger, month_ends_between(ledger.start, period_end)):
        xero = ledger.balances.get(row["end"].isoformat())
        xero = Decimal(xero) if xero is not None else None
        months.append({"end": row["end"], "opening": shown(account, row["opening"]),
                       "added": shown(account, row[Movement.ADDITION]),
                       "reversed": shown(account, row[Movement.REVERSAL]),
                       "paid": shown(account, row[Movement.PAYMENT] + row[Movement.ADJUSTMENT]),
                       "closing": shown(account, row["closing"]), "xero": shown(account, xero),
                       "difference": shown(account, xero - row["closing"]) if xero is not None else None})
    categories = [{"name": r["name"], "opening": shown(account, r["opening"]), "added": shown(account, r["added"]),
                   "released": shown(account, r["released"]), "closing": shown(account, r["closing"]),
                   "items": r["items"]} for r in by_category(ledger, period_end)]
    items = [{"item": item, "balance": shown(account, bal), "flags": flags(ledger, item, bal, period_end)}
             for item, bal in open_at(ledger, period_end)]
    total = sum((i["balance"] for i in items), ZERO)
    xero = ledger.balances.get(period_end.isoformat())
    proposed = OpenItemPosting.objects.filter(ledger=ledger, confidence=Confidence.PROPOSED,
                                              date__lte=period_end).count()
    return {"months": months, "categories": categories, "items": items, "total": total,
            "xero": shown(account, Decimal(xero)) if xero is not None else None, "proposed": proposed}
