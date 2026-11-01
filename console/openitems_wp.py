"""/reconcile for an account reconciled as open items (accruals, prepayments, deposits...): its own ledger in iLA.

``/reconcile 2000 --seed 30 Jun 2026 << seed.xlsx``      start from the accountant's breakdown at 30 Jun 2026
``/reconcile 1140 --1 Jul 2025_30 Jun 2026``            iLA works out its own breakdown at 30 Jun 2025 (reading
                                                         --lookback 3 months before it) and builds to 30 Jun 2026
``/reconcile 1140 --1 Jul 2025_30 Jun 2026 << gl.xlsx`` the same from an uploaded GL instead of reading Xero
``/reconcile 2000 --Sep 2026``                           the working paper at a month end, built up to it

The working paper stays open; plain answers change it (see HELP). Categories and descriptions can also be changed
in admin (Accounting > Open-item categories / descriptions). See accounting/openitems.py for how it is built.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from accounting import openitem_jobs
from accounting.models import (CategoryBehaviour, OpenItem, OpenItemCategory, OpenItemDecision, OpenItemLedger,
                               OpenItemPosting)
from accounting.openitem_files import FileProblem, read_gl, read_seed
from accounting.openitems import UNASSIGNED, balances_at, natural_sign, report, roll_forward
from core.models import AuditLog, ReconMethod

from .output import DATE, MONEY, NUMBER, Column, Note, Table
from .periods import fmt
from .registry import CommandError

ZERO = Decimal("0")
CENT = Decimal("0.01")
HELP = ("Type: items (or items all, items UTL) · why D2000-1005 · move <txn> D2000-0003 · confirm <txn> (or confirm "
        "all) · proposed · months · categories · category UTL name|words|accounts|behaviour|standing|scope <value> · "
        "category C11 merge LIC · category add UTL Utilities · "
        "item D2000-1005 description|category|counterparty|reference|note <value> · "
        "rebuild · refresh (show the latest) · reread (read Xero again) · prepared · reviewed · done.")
BEHAVIOURS = {c.value: c for c in CategoryBehaviour} | {c.label.split(" (")[0].lower(): c for c in CategoryBehaviour}
ITEM_FIELDS = {"description": "description", "text": "description", "category": "category", "cat": "category",
               "counterparty": "counterparty", "supplier": "counterparty", "reference": "reference", "ref": "reference",
               "note": "note", "notes": "note"}


def ledger_for(account):
    return OpenItemLedger.objects.filter(account=account).select_related("account").first()


def display(account, amount):
    """A debit-positive amount the way the balance sheet shows the account."""
    return amount * natural_sign(account)


def _audit(ctx, ledger, what, extra=None):
    AuditLog.objects.create(organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
                            input=f"/reconcile {ledger.account.code} open items: {what}"[:2000],
                            target_type="accounting.openitemledger", target_id=str(ledger.pk), result=extra or {})


def _use_open_items(account):
    if account.recon_method != ReconMethod.OPEN_ITEMS:
        was = account.get_recon_method_display() or "not set"
        account.recon_method = ReconMethod.OPEN_ITEMS
        account.save(update_fields=["recon_method", "updated_at"])
        return f"{account} is now reconciled as open items (it was {was})."
    return ""


def _start_build(ctx, ledger, to, *, copy=True):
    started = openitem_jobs.run(ledger, to, source_factory=lambda org: ctx.source, copy=copy)
    if not started:
        return "A build is already running for this account; type refresh in a minute to see it."
    ledger.refresh_from_db()
    if ledger.job_status == openitem_jobs.RUNNING:
        return "Building in the background (reading Xero can take a minute or two). Type refresh to see it."
    return ""


def setup(ctx, account, params, period, period_end, upload):
    """Handle the open-item forms of /reconcile <account>. Returns (period end, message) for the working paper."""
    ledger = ledger_for(account)
    seed_day = params.values.get("seed")
    lookback = params.values.get("lookback")
    multi = period is not None and (period.start.year, period.start.month) != (period.end.year, period.end.month)
    messages = []
    if seed_day is not None:
        if upload is None:
            raise CommandError(f"Attach the seed: /reconcile {account.code} --seed {fmt(seed_day)} << seed.xlsx (the "
                               "breakdown of the balance at that date).")
        if period is None or period.end <= seed_day:
            period_end = max(period_end, seed_day)
        opening = openitem_jobs.xero_balance(ctx.source, account, seed_day)
        try:
            categories, rows, notes, source = read_seed(upload, account, opening)
        except FileProblem as exc:
            raise CommandError(str(exc)) from None
        source |= {"by": ctx.user.get_username() if ctx.user else "", "at": timezone.now().isoformat(timespec="seconds")}
        if ledger is None:
            ledger = OpenItemLedger(organisation=ctx.organisation, account=account)
        ledger.opening_date, ledger.opening_balance = seed_day, opening
        ledger.seed, ledger.seed_rows, ledger.seed_source = OpenItemLedger.Seed.UPLOAD, rows, source
        ledger.built_to, ledger.gl_rows, ledger.gl_source = None, [], {}
        ledger.save()
        for n, c in enumerate(categories, 1):
            cat, made = OpenItemCategory.objects.get_or_create(
                organisation=ctx.organisation, ledger=ledger, code=c["code"],
                defaults={"name": c["name"] or c["code"], "scope": c["scope"], "order": n,
                          "words": _words_from(c["name"], c["scope"])})
        given = sum((Decimal(r["amount"]) for r in rows), ZERO)
        messages.append(f"Seed from {upload.name}: {len(rows)} description(s), {display(account, given):,.2f}, "
                        f"against Xero's {display(account, opening):,.2f} at {fmt(seed_day)}"
                        + (f"; {display(account, opening - given):,.2f} is 'Opening not analysed'."
                           if abs(opening - given) >= CENT else ".") + (" " + " ".join(notes) if notes else ""))
        _audit(ctx, ledger, "seed uploaded", {"file": source["file"], "sha256": source["sha256"], "rows": len(rows)})
    elif multi or (upload is not None and ledger is None):
        if period is None:
            raise CommandError(f"Give the window with the GL, e.g. /reconcile {account.code} --1 Jul 2025_30 Jun 2026 "
                               "<< gl.xlsx.")
        opening_day = period.start - timedelta(days=1)
        if ledger is not None and ledger.seed == OpenItemLedger.Seed.UPLOAD and ledger.opening_date != opening_day:
            raise CommandError(f"{account} starts from the seed you gave at {fmt(ledger.opening_date)}. Use "
                               f"/reconcile {account.code} --{period.end:%b %Y}, or upload a new seed with --seed.")
        if ledger is None:
            ledger = OpenItemLedger(organisation=ctx.organisation, account=account, seed=OpenItemLedger.Seed.ILA,
                                    opening_date=opening_day)
        if ledger.opening_date != opening_day:
            ledger.opening_date, ledger.built_to = opening_day, None
        if lookback:
            ledger.lookback_months = lookback
        ledger.save()
        if ledger.seed == OpenItemLedger.Seed.ILA:
            messages.append(f"iLA works out the breakdown at {fmt(opening_day)} by reading the "
                            f"{ledger.lookback_months} month(s) before it (--lookback 12 to read more).")
    elif ledger is None:
        raise CommandError(
            f"{account} has no starting point yet. Either give the breakdown of its balance at a date: /reconcile "
            f"{account.code} --seed 30 Jun 2026 << seed.xlsx; or let iLA work one out over a window: /reconcile "
            f"{account.code} --1 Jul 2025_30 Jun 2026 (add << gl.xlsx to use an exported GL instead of reading Xero).")
    elif lookback:
        ledger.lookback_months = lookback
        ledger.built_to = None
        ledger.save(update_fields=["lookback_months", "built_to", "updated_at"])
    if upload is not None and seed_day is None:
        try:
            rows, source = read_gl(upload, account)
        except FileProblem as exc:
            raise CommandError(str(exc)) from None
        ledger.gl_rows, ledger.gl_source, ledger.built_to = rows, source, None
        ledger.save(update_fields=["gl_rows", "gl_source", "built_to", "updated_at"])
        messages.append(f"GL from {upload.name}: {len(rows)} transaction(s), {source['first']} to {source['last']}; "
                        "used instead of reading Xero.")
        _audit(ctx, ledger, "GL uploaded", {"file": source["file"], "sha256": source["sha256"], "rows": len(rows)})
    if period_end < ledger.opening_date:
        raise CommandError(f"{account} starts at {fmt(ledger.opening_date)}; reconcile a month end after it.")
    messages.append(_use_open_items(account))
    if ledger.built_to is None or ledger.built_to < period_end:
        messages.append(_start_build(ctx, ledger, period_end, copy=True))
    return period_end, " ".join(m for m in messages if m)


def _words_from(name, scope):
    words = [w.strip().lower() for w in re.split(r"[,;/]| and ", f"{scope}") if len(w.strip()) >= 3]
    return ", ".join(dict.fromkeys(words))[:255]


# --- The working paper ------------------------------------------------------------------------------------------

def support(rec):
    """The open descriptions' total at the period end, the way the balance sheet shows it; None until built."""
    ledger = ledger_for(rec.account)
    if ledger is None or ledger.built_to is None or ledger.built_to < rec.period_end:
        return None
    if rec.period_end < ledger.opening_date:
        return None
    total = sum(balances_at(ledger, rec.period_end).values(), ZERO)
    return display(rec.account, total).quantize(CENT)


def blocks(result, rec):
    """The open-item parts of the working paper: the roll-forward, the month by category and the open
    descriptions."""
    account, ledger = rec.account, ledger_for(rec.account)
    if ledger is None:
        return
    if ledger.job_status == openitem_jobs.RUNNING:
        result.add(Note(f"Building: {ledger.job_message} Type refresh in a minute to see it.", "warning"))
        return
    if ledger.job_status == openitem_jobs.FAILED:
        result.add(Note(f"The last build failed: {ledger.job_message} Type refresh to try again.", "warning"))
    if ledger.built_to is None or ledger.built_to < rec.period_end:
        result.add(Note("Not built up to this month end yet: type refresh.", "warning"))
        return
    seed = ("your seed " + ledger.seed_source.get("file", "") if ledger.seed == OpenItemLedger.Seed.UPLOAD
            else f"iLA's breakdown from the {ledger.lookback_months} month(s) before it")
    source = f"the GL {ledger.gl_source.get('file', '')}" if ledger.gl_rows else "iLA's copy of Xero"
    result.add(Note(f"Starts at {fmt(ledger.opening_date)} from {seed}; postings from {source} up to "
                    f"{fmt(ledger.built_to)}." + (f" {ledger.job_message}" if ledger.job_message else "")))
    data = report(ledger, rec.period_end)
    table = result.add(Table([Column("Month end", DATE), Column("Opening", MONEY), Column("Added", MONEY),
                              Column("Reversed", MONEY), Column("Billed / other", MONEY), Column("Closing", MONEY),
                              Column("Xero", MONEY), Column("Difference", MONEY)],
                             title="Roll-forward: opening + movements = closing, against Xero"))
    for m in data["months"]:
        table.add(m["end"], m["opening"], m["added"], m["reversed"], m["paid"], m["closing"], m["xero"],
                  m["difference"])
    table = result.add(Table([Column("Category"), Column("Opening", MONEY), Column("Added", MONEY),
                              Column("Released", MONEY), Column("Closing", MONEY), Column("Open", NUMBER)],
                             title=f"By category, {rec.period_end:%B %Y}"))
    totals = [ZERO] * 4
    for row in data["categories"]:
        values = [row[k] for k in ("opening", "added", "released", "closing")]
        totals = [a + b for a, b in zip(totals, values)]
        table.add(row["name"], *values, row["items"])
    table.add("Total", *totals, None, total=True)
    table = result.add(Table([Column("Code"), Column("Category"), Column("Description"), Column("Reference"),
                              Column("Raised", DATE), Column("Balance", MONEY), Column("Flags")],
                             title=f"Open descriptions at {fmt(rec.period_end)}"))
    for row in data["items"]:
        item = row["item"]
        table.add(item.code, item.category.code if item.category else UNASSIGNED, item.description,
                  item.reference, item.raised_on, row["balance"], " ".join(row["flags"]))
    table.add("", "", "Total", "", None, data["total"], "", total=True)
    if data["proposed"]:
        result.add(Note(f"{data['proposed']} link(s) were proposed rather than certain: type proposed to check them, "
                        "then confirm or move them.", "warning"))


def _ledger(rec):
    ledger = ledger_for(rec.account)
    if ledger is None:
        raise CommandError("This account has no open-item ledger yet.")
    return ledger


def _item(ledger, text):
    text = text.strip()
    if not text:
        raise CommandError("Give a description code, e.g. D2000-1005 (or just 1005).")
    found = OpenItem.objects.filter(ledger=ledger, code__iexact=text).first()
    if found is None and text.isdigit():
        found = next((i for i in OpenItem.objects.filter(ledger=ledger, code__endswith=f"-{int(text):04d}")), None)
    if found is None:
        raise CommandError(f'No description "{text}". Type items to list them.')
    return found


def _category(ledger, text, *, required=True):
    found = OpenItemCategory.objects.filter(ledger=ledger, code__iexact=text.strip()).first()
    if found is None and required:
        codes = ", ".join(OpenItemCategory.objects.filter(ledger=ledger).values_list("code", flat=True)[:30])
        raise CommandError(f'No category "{text.strip()}". Categories: {codes or "none yet"}. '
                           "Add one with category add <CODE> <name>.")
    return found


def answer(ctx, rec, word, rest, raw_rest):
    """A plain answer in an open-item working paper: (a Result to show instead, or None; the message for the
    working paper). (None, None) if the word isn't one of the open-item ones."""
    ledger = _ledger(rec)
    if word == "items":
        return _items_result(ledger, rec, rest.strip()), ""
    if word == "categories":
        return _categories_result(ledger), ""
    if word == "category":
        return None, _category_answer(ctx, ledger, raw_rest)
    if word == "item":
        return None, _item_answer(ctx, ledger, raw_rest)
    if word == "why":
        return _why(ledger, rec, raw_rest), ""
    if word == "proposed":
        return _proposed(ledger, rec), ""
    if word == "months":
        return _months(ledger, rec), ""
    if word in ("move", "confirm", "unpin"):
        return None, _decide(ctx, ledger, rec, word, raw_rest)
    if word == "rebuild":
        message = _start_build(ctx, ledger, max(rec.period_end, ledger.built_to or rec.period_end), copy=False)
        return None, message or "Rebuilt from iLA's copy, with your decisions and edits applied."
    if word in ("refresh", "show"):
        if ledger.job_status == openitem_jobs.RUNNING:
            return None, "Still building; type refresh again in a minute."
        if ledger.built_to is None or ledger.built_to < rec.period_end:
            return None, _start_build(ctx, ledger, rec.period_end, copy=True) or "Built."
        return None, ""
    if word == "reread":
        message = _start_build(ctx, ledger, max(rec.period_end, ledger.built_to or rec.period_end), copy=True)
        return None, message or "Read Xero again and rebuilt."
    return None, None


def _items_result(ledger, rec, which):
    from .output import Result

    account = rec.account
    result = Result(f"{account}: descriptions", subtitle="item <code> <field> <value> changes one; why <code> shows "
                                                         "its postings")
    bal = balances_at(ledger, rec.period_end)
    items = OpenItem.objects.filter(ledger=ledger).select_related("category").order_by("raised_on", "code")
    if which and which != "all":
        cat = _category(ledger, which)
        items = items.filter(category=cat)
    if which != "all":
        items = [i for i in items if abs(bal.get(i.pk, ZERO)) >= CENT]
    table = result.add(Table([Column("Code"), Column("Category"), Column("Description"), Column("Counterparty"),
                              Column("Reference"), Column("Raised", DATE), Column("Original", MONEY),
                              Column(f"Balance {fmt(rec.period_end)}", MONEY), Column("Bill"), Column("Edited")]))
    for i in items:
        bill = f"{i.bill.get('number')} {i.bill.get('date')}" if i.bill else ""
        table.add(i.code, i.category.code if i.category else UNASSIGNED, i.description, i.counterparty, i.reference,
                  i.raised_on, display(account, i.original_amount), display(account, bal.get(i.pk, ZERO)), bill,
                  ", ".join(i.edited))
    return result


def _categories_result(ledger):
    from .output import Result

    result = Result(f"{ledger.account}: categories",
                    subtitle="category <CODE> name|words|accounts|behaviour|standing|scope <value> changes one")
    table = result.add(Table([Column("Code"), Column("Name"), Column("Behaviour"), Column("Standing"),
                              Column("Accounts"), Column("Words"), Column("Scope"), Column("Descriptions", NUMBER)]))
    for c in OpenItemCategory.objects.filter(ledger=ledger):
        table.add(c.code, c.name + ("" if c.is_active else " (off)"), c.get_behaviour_display(),
                  "Yes" if c.standing else "", c.accounts, c.words, c.scope, c.items.count())
    result.add(Note("A line goes to the first category listing a word it contains, else the one listing its "
                    "account, else the one whose name shares a word with the account's name. After changing words "
                    "or accounts, type rebuild to sort the postings again."))
    return result


def _category_answer(ctx, ledger, text):
    parts = text.split(None, 2)
    if not parts:
        raise CommandError("e.g. category add UTL Utilities, or category UTL words electricity, gas.")
    if parts[0].lower() == "add":
        if len(parts) < 3:
            raise CommandError("Give a code and a name: category add UTL Utilities.")
        code = parts[1].upper()[:12]
        if OpenItemCategory.objects.filter(ledger=ledger, code__iexact=code).exists():
            raise CommandError(f"There's already a category {code}.")
        order = OpenItemCategory.objects.filter(ledger=ledger).count() + 1
        OpenItemCategory.objects.create(organisation=ledger.organisation, ledger=ledger, code=code,
                                        name=parts[2][:100], order=order)
        _audit(ctx, ledger, f"category {code} added")
        return f"Category {code} {parts[2]} added. Give it words or accounts, then rebuild."
    cat = _category(ledger, parts[0])
    if len(parts) < 2:
        raise CommandError(f"What should change? e.g. category {cat.code} name <name>.")
    what = parts[1].lower()
    value = parts[2].strip() if len(parts) > 2 else ""
    if what in ("off", "on"):
        cat.is_active = what == "on"
    elif what == "name":
        cat.name = value[:100] or cat.name
    elif what == "scope":
        cat.scope = value[:255]
    elif what == "words":
        cat.words = value[:255]
    elif what == "accounts":
        cat.accounts = " ".join(value.replace(",", " ").split())[:255]
    elif what == "behaviour":
        found = BEHAVIOURS.get(value.lower().replace("-", " "))
        if found is None:
            raise CommandError("Behaviour is one of: " + ", ".join(c.label.split(" (")[0].lower()
                                                                  for c in CategoryBehaviour) + ".")
        cat.behaviour = found
    elif what == "standing":
        cat.standing = value.lower() in ("yes", "y", "on", "true", "1")
    elif what == "merge":
        into = _category(ledger, value)
        if into.pk == cat.pk:
            raise CommandError("Merge it into a different category.")
        moved = 0
        for item in OpenItem.objects.filter(ledger=ledger, category=cat):
            item.category = into
            item.edited = sorted(set(item.edited) | {"category"})
            item.save(update_fields=["category", "edited", "updated_at"])
            moved += 1
        into.accounts = " ".join(dict.fromkeys(into.account_codes() + cat.account_codes()))[:255]
        into.words = ", ".join(dict.fromkeys(into.word_list() + cat.word_list()))[:255]
        into.save()
        cat.is_active, cat.accounts, cat.words = False, "", ""
        cat.save()
        _audit(ctx, ledger, f"category {cat.code} merged into {into.code}", {"descriptions": moved})
        return (f"Category {cat.code} merged into {into.code}: {moved} description(s), its accounts and words moved; "
                f"{cat.code} is switched off.")
    else:
        raise CommandError("Change name, scope, words, accounts, behaviour, standing, on or off, or merge it into "
                           "another: category C11 merge LIC.")
    cat.save()
    _audit(ctx, ledger, f"category {cat.code} {what} changed", {"value": value})
    return (f"Category {cat.code} updated." + (" Type rebuild to sort the postings again."
                                               if what in ("words", "accounts", "standing", "on", "off") else ""))


def _item_answer(ctx, ledger, text):
    parts = text.split(None, 2)
    if len(parts) < 2:
        raise CommandError("e.g. item D2000-1005 category UTL, or item 1005 description June electricity.")
    item = _item(ledger, parts[0])
    field = ITEM_FIELDS.get(parts[1].lower())
    if field is None:
        raise CommandError("Change description, category, counterparty, reference or note.")
    value = parts[2].strip() if len(parts) > 2 else ""
    if field == "category":
        item.category = None if value.lower() in ("", "none", "unassigned") else _category(ledger, value)
    else:
        setattr(item, field, value[:255] if field != "note" else value)
    if field != "note":
        item.edited = sorted(set(item.edited) | {field})
    item.save()
    _audit(ctx, ledger, f"description {item.code} {field} changed", {"value": value})
    return f"{item.code} updated; a rebuild keeps it."


def _posting_table(account, postings, title):
    table = Table([Column("Txn"), Column("Date", DATE), Column("Movement"), Column("Amount", MONEY),
                   Column("Description"), Column("Record"), Column("Line(s)"), Column("From lines"),
                   Column("How linked"), Column("Confidence"), Column("Xero")], title=title)
    for p in postings:
        table.add(p.txn, p.date, p.get_movement_display(), display(account, p.amount), p.item.code,
                  f"{p.record} {p.record_number}".strip(), p.lines, p.from_lines, p.basis, p.get_confidence_display(),
                  p.xero_url)
    return table


def _why(ledger, rec, text):
    from .output import Result

    item = _item(ledger, text)
    result = Result(f"{item.code} {item.description}",
                    subtitle=f"{item.category or UNASSIGNED} · {item.get_origin_display()} · raised by {item.raised_by}")
    postings = item.postings.select_related("item").order_by("seq")
    result.add(_posting_table(rec.account, postings, "Postings"))
    if item.bill:
        result.add(Note(f"Bill {item.bill.get('number')} from {item.bill.get('supplier')} dated "
                        f"{item.bill.get('date')}, {Decimal(item.bill.get('net') or 0):,.2f} ex GST, is in Xero."))
    result.add(Note("move <txn> <code> moves a posting to another description; confirm <txn> keeps a proposed link."))
    return result


def _proposed(ledger, rec):
    from .output import Result

    postings = (OpenItemPosting.objects.filter(ledger=ledger, confidence=OpenItemPosting.Confidence.PROPOSED)
                .select_related("item").order_by("seq"))
    result = Result(f"{rec.account}: proposed links", subtitle="confirm <txn> (or confirm all) keeps them; move <txn> "
                                                               "<code> puts one elsewhere")
    result.add(_posting_table(rec.account, postings, ""))
    return result


def _months(ledger, rec):
    from .output import Result

    account = rec.account
    ends = openitem_jobs.month_ends(ledger.start, min(rec.period_end, ledger.built_to or rec.period_end))
    result = Result(f"{account}: roll-forward", subtitle="opening + movements = closing, against Xero's balance")
    table = result.add(Table([Column("Month end", DATE), Column("Opening", MONEY), Column("Added", MONEY),
                              Column("Reversed", MONEY), Column("Billed / paid", MONEY), Column("Closing", MONEY),
                              Column("Xero", MONEY), Column("Not in iLA's copy", MONEY)]))
    for row in roll_forward(ledger, ends):
        xero = ledger.balances.get(row["end"].isoformat())
        xero = Decimal(xero) if xero is not None else None
        table.add(row["end"], *(display(account, row[k]) for k in ("opening", "addition", "reversal", "payment",
                                                                   "closing")),
                  display(account, xero) if xero is not None else None,
                  display(account, xero - row["closing"]) if xero is not None else None)
    return result


def _decide(ctx, ledger, rec, word, text):
    parts = text.split()
    if word == "confirm" and parts and parts[0].lower() == "all":
        postings = list(OpenItemPosting.objects.filter(ledger=ledger, confidence=OpenItemPosting.Confidence.PROPOSED,
                                                       date__lte=rec.period_end).select_related("item"))
        for p in postings:
            OpenItemDecision.objects.update_or_create(
                organisation=ledger.organisation, ledger=ledger, txn=p.txn,
                defaults={"kind": OpenItemDecision.Kind.CONFIRM, "item_code": p.item.code, "decided_by": ctx.user})
        OpenItemPosting.objects.filter(pk__in=[p.pk for p in postings]).update(
            confidence=OpenItemPosting.Confidence.CONFIRMED)
        _audit(ctx, ledger, f"{len(postings)} proposed link(s) confirmed")
        return f"{len(postings)} link(s) confirmed."
    if not parts:
        raise CommandError("Give the posting's Txn, e.g. confirm MJ-047b217d-L6 (see why <code> or proposed).")
    posting = OpenItemPosting.objects.filter(ledger=ledger, txn__iexact=parts[0]).select_related("item").first()
    if posting is None:
        raise CommandError(f'No posting "{parts[0]}". Its Txn is shown by why <code> and proposed.')
    if word == "unpin":
        OpenItemDecision.objects.filter(ledger=ledger, txn=posting.txn).delete()
        message = _start_build(ctx, ledger, ledger.built_to or rec.period_end, copy=False)
        return message or f"{posting.txn} is back to iLA's own link."
    if word == "confirm":
        OpenItemDecision.objects.update_or_create(
            organisation=ledger.organisation, ledger=ledger, txn=posting.txn,
            defaults={"kind": OpenItemDecision.Kind.CONFIRM, "item_code": posting.item.code, "decided_by": ctx.user})
        posting.confidence = OpenItemPosting.Confidence.CONFIRMED
        posting.save(update_fields=["confidence", "updated_at"])
        _audit(ctx, ledger, f"{posting.txn} confirmed on {posting.item.code}")
        return f"{posting.txn} confirmed on {posting.item.code}."
    if len(parts) < 2:
        raise CommandError("Give the posting and where it goes: move MJ-047b217d-L6 D2000-0003.")
    target = _item(ledger, parts[1])
    OpenItemDecision.objects.update_or_create(
        organisation=ledger.organisation, ledger=ledger, txn=posting.txn,
        defaults={"kind": OpenItemDecision.Kind.MOVE, "item_code": target.code, "decided_by": ctx.user})
    _audit(ctx, ledger, f"{posting.txn} moved to {target.code}")
    message = _start_build(ctx, ledger, ledger.built_to or rec.period_end, copy=False)
    return message or f"{posting.txn} moved to {target.code} and the ledger rebuilt."

