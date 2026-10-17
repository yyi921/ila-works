"""/_createInvoice: a guided conversation that builds one customer invoice as an import file.

Steps: invoice set-up (if the organisation has several) → customer (searched in Xero) → event or job → billing
information (files chosen with <<, or typed notes) → draft on screen → tweaks in plain English → approve.

Division of labour: the language model reads the documents and proposes lines and questions; iLA decides the
account codes and tax rates (from the set-up's items, past invoices and defaults), calculates every amount and
total, checks the draft against the organisation's Xero data and writes the file. Nothing is written to Xero: the
approved file is imported by a person. The draft is saved after every answer, so it can be resumed.
"""

import difflib
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.urls import reverse

from core.invoicing import InvoiceFileError, build_csv
from core.models import AuditLog, ImportFile, InvoiceSetup, LineAmountType

from . import llm
from .models import InvoiceDraft
from .output import MONEY, NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from .registry import CommandError

CENT = Decimal("0.01")
MAX_FILE_BYTES = 10 * 1024 * 1024
DONE_WORDS = {"done", "finished", "that's all", "thats all", "no more"}
# "go ahead", "create the draft", "draft it to your best knowledge"… (but not details such as "8 hours on 1 Oct")
PROCEED_RE = re.compile(r"\b(done|go ahead|proceed|continue|draft|create|prepare|ready|next|that'?s (all|it))\b",
                        re.IGNORECASE)
APPROVE_WORDS = {"approve", "approved", "ok approve", "yes approve"}
CANCEL_WORDS = {"cancel", "/cancel", "stop", "quit"}
NEW_CUSTOMER = "__new__"
AS_TYPED = "__typed__"


# --- Entry points -----------------------------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return InvoiceDraft.objects.filter(organisation=ctx.organisation, user=ctx.user,
                                       status=InvoiceDraft.Status.OPEN).first()


def start(ctx, cmd, custom):
    """``/_createInvoice [customer] [--new]``: resume the open draft, or start one."""
    if cmd.input_file:
        raise CommandError(f"Start with /{custom.name}, then choose the files when iLA asks for them.")
    unknown = [p for p in cmd.params if p.strip().lower() != "new"]
    if unknown:
        raise CommandError(f'/{custom.name} only takes --new (start again). "--{unknown[0]}" isn\'t an option.')
    existing = open_draft(ctx)
    if existing and not cmd.params:
        return show(ctx, existing, intro=f"Picking up your open draft. Type cancel to drop it, or start again with "
                                         f"/{custom.name} --new.")
    if existing:
        existing.status = InvoiceDraft.Status.CANCELLED
        existing.save()
    setups = list(InvoiceSetup.objects.filter(organisation=ctx.organisation, is_active=True))
    if not setups:
        raise CommandError(f"{ctx.organisation.name} has no invoice set-up yet. Add one on the organisation's admin "
                           "page (Invoice set-ups), then try again.")
    draft = InvoiceDraft.objects.create(organisation=ctx.organisation, user=ctx.user, command=custom, data={})
    if len(setups) == 1:
        draft.setup = setups[0]
        draft.step = InvoiceDraft.Step.CUSTOMER
        draft.save()
        if cmd.text:
            return _customer(ctx, draft, cmd.text)
        return show(ctx, draft)
    draft.data["choices"] = [{"label": str(s), "value": s.pk} for s in setups]
    draft.data["customer_hint"] = cmd.text
    draft.save()
    return show(ctx, draft)


def cancel(ctx):
    draft = open_draft(ctx)
    if draft is None:
        return Result("Nothing to cancel", subtitle="There's no open /tag preview, /createTag or invoice draft.")
    draft.status = InvoiceDraft.Status.CANCELLED
    draft.save()
    return Result("Draft cancelled", subtitle=f"{_customer_name(draft) or 'The draft'} was dropped. Nothing was "
                                              "written anywhere.")


def answer(ctx, draft, text, files=()):
    """Plain input (no "/") while a draft is open: an answer to the current question."""
    text = (text or "").strip()
    if text.startswith("<<"):
        text = ""  # "<< file.pdf" only names the files being uploaded
    if text.lower() in CANCEL_WORDS:
        return cancel(ctx)
    step = draft.step
    if step == InvoiceDraft.Step.SETUP:
        return _setup(ctx, draft, text)
    if step == InvoiceDraft.Step.CUSTOMER:
        return _customer(ctx, draft, text)
    if step == InvoiceDraft.Step.EVENT:
        return _event(ctx, draft, text)
    if step == InvoiceDraft.Step.SOURCES:
        return _sources(ctx, draft, text, files)
    return _review(ctx, draft, text, files)


# --- Steps ------------------------------------------------------------------------------------------------------

def _pick(draft, text):
    choices = draft.data.get("choices") or []
    if text.isdigit() and 1 <= int(text) <= len(choices):
        return choices[int(text) - 1]
    return None


def _setup(ctx, draft, text):
    choice = _pick(draft, text)
    if choice is None:
        return show(ctx, draft, intro="Type the number of the set-up to use.")
    draft.setup = InvoiceSetup.objects.get(pk=choice["value"], organisation=ctx.organisation)
    draft.step = InvoiceDraft.Step.CUSTOMER
    draft.data["choices"] = []
    hint = draft.data.pop("customer_hint", "")
    draft.save()
    return _customer(ctx, draft, hint) if hint else show(ctx, draft)


def _customer(ctx, draft, text):
    choice = _pick(draft, text)
    if choice:
        if choice["value"] == NEW_CUSTOMER:
            customer = {"name": choice["name"], "new": True}
        else:
            customer = choice["value"]
        return _set_customer(ctx, draft, customer)
    if not text:
        return show(ctx, draft, intro="Type part of the customer's name and I'll search Xero.")
    if text.lower().startswith("new "):
        return _set_customer(ctx, draft, {"name": text[4:].strip(), "new": True})
    found = ctx.source.customers(text)
    found.sort(key=lambda c: _similarity(text, c["name"]), reverse=True)
    draft.data["choices"] = (
        [{"label": c["name"] + (f" · {c['email']}" if c["email"] else ""), "value": c} for c in found[:8]]
        + [{"label": f'New customer "{text}" (Xero will create the contact on import)', "value": NEW_CUSTOMER,
            "name": text}])
    draft.save()
    intro = (f'Customers in Xero matching "{text}". Type a number, or search again.' if found
             else f'No customer in Xero matches "{text}". Search again, or pick 1 to use it as a new customer.')
    return show(ctx, draft, intro=intro)


def _set_customer(ctx, draft, customer):
    draft.data["customer"] = customer
    draft.data["choices"] = []
    draft.step = InvoiceDraft.Step.EVENT
    suggestions = _event_suggestions(ctx, draft)
    draft.data["choices"] = [{"label": s, "value": s} for s in suggestions]
    draft.save()
    return show(ctx, draft)


def _event_suggestions(ctx, draft):
    customer = draft.data.get("customer") or {}
    refs = []
    for inv in _recent(ctx):
        if (customer.get("id") and inv["contact_id"] == customer["id"]) or inv["contact"] == customer.get("name"):
            if inv["reference"] and inv["reference"] not in refs:
                refs.append(inv["reference"])
    return refs[:5]


def _event(ctx, draft, text):
    choice = _pick(draft, text)
    if choice:
        if choice["value"] == AS_TYPED:
            return _set_event(ctx, draft, {"name": choice["name"], "tracking_option": ""})
        if isinstance(choice["value"], dict):
            return _set_event(ctx, draft, choice["value"])
        return _set_event(ctx, draft, {"name": choice["value"], "tracking_option": ""})
    if not text:
        return show(ctx, draft, intro="Type the event or job name, or pick a number.")
    options = _tracking_options(ctx, draft)
    if not options:
        return _set_event(ctx, draft, {"name": text, "tracking_option": ""})
    ranked = sorted(options, key=lambda o: _similarity(text, o), reverse=True)
    exact = [o for o in ranked if o.lower() == text.lower()]
    if exact:
        return _set_event(ctx, draft, {"name": text, "tracking_option": exact[0]})
    close = [o for o in ranked if _similarity(text, o) >= 0.5][:6]
    category = draft.setup.tracking_name
    draft.data["choices"] = (
        [{"label": f"{o} ({category})", "value": {"name": o, "tracking_option": o}} for o in close]
        + [{"label": f'"{text}" without a {category} tag', "value": AS_TYPED, "name": text}])
    draft.save()
    intro = (f'{category} options in Xero close to "{text}". Type a number.' if close
             else f'No {category} option in Xero is close to "{text}". Pick the last option, or type another name.')
    return show(ctx, draft, intro=intro)


def _set_event(ctx, draft, event):
    draft.data["event"] = event
    draft.data["choices"] = []
    draft.step = InvoiceDraft.Step.SOURCES
    draft.save()
    return show(ctx, draft)


def _sources(ctx, draft, text, files):
    notes = []
    if files:
        notes += _read_files(draft, files)
    if text and (text.lower() in DONE_WORDS or (PROCEED_RE.search(text) and not re.search(r"\d", text))):
        if not draft.data.get("sources"):
            return show(ctx, draft, intro="I have nothing to bill from yet. Choose files with <<, or type the "
                                          "details (e.g. hours worked per day and the rate).")
        return _compose(ctx, draft, request=text if text.lower() not in DONE_WORDS else "")
    if text and not files:
        draft.data.setdefault("sources", []).append(
            {"file": "Typed by you", "type": "note", "summary": text, "facts": {"note": text}})
        draft.save()
        notes.append("Noted.")
    return show(ctx, draft, intro=" ".join(notes))


def _review(ctx, draft, text, files):
    if files:
        notes = _read_files(draft, files)
        return _compose(ctx, draft, instruction=(text or "") + "\nNew billing information was added: "
                                                + "; ".join(notes), intro=" ".join(notes))
    if not text:
        return show(ctx, draft, intro="Type changes in plain English, add files with <<, or type approve.")
    if text.lower() in APPROVE_WORDS:
        return _approve(ctx, draft)
    return _compose(ctx, draft, instruction=text)


# --- Reading documents ------------------------------------------------------------------------------------------

READ_SYSTEM = """You read billing documents for a business that invoices its customers (for example an events \
venue or a labour hire firm) so a person can prepare an accurate invoice. Record what the document says, \
faithfully: never invent or estimate a value that isn't there. Quote prices exactly as written and say whether the \
document states them including or excluding tax. Give page numbers where you can. Include everything that could be \
billed: fees, hourly or daily rates with their hours, per-ticket or percentage fees, charges that only apply in \
some cases, and minimums. Record deposits, payments and settlements separately from charges."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": ["agreement", "order", "quote", "supplier_bill", "timesheet",
                                                     "settlement", "invoice", "note", "other"]},
        "title": {"type": "string"},
        "summary": {"type": "string", "description": "Two sentences: what this is and what it means for billing."},
        "parties": {"type": "string"},
        "event_or_job": {"type": "string"},
        "dates": {"type": "string"},
        "prices_include_tax": {"type": "string", "enum": ["inclusive", "exclusive", "mixed", "unknown"]},
        "charges": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"}, "quantity": {"type": "string"}, "unit": {"type": "string"},
            "rate": {"type": "string"}, "amount": {"type": "string"}, "tax": {"type": "string"},
            "condition": {"type": "string", "description": "When it applies, if conditional."},
            "page": {"type": "string"}}, "required": ["description"]}},
        "payments": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"}, "amount": {"type": "string"}, "page": {"type": "string"}},
            "required": ["description", "amount"]}},
        "terms": {"type": "array", "items": {"type": "string"},
                  "description": "Billing terms that could mean extra charges (fees, minimums, surcharges)."},
    },
    "required": ["document_type", "title", "summary", "prices_include_tax", "charges"],
}


def _read_files(draft, files):
    notes = []
    for upload in files:
        name = getattr(upload, "name", "file")
        if upload.size > MAX_FILE_BYTES:
            notes.append(f"{name} is over 10 MB and wasn't read.")
            continue
        content = upload.read()  # read and discarded: only what the model extracts is kept
        facts = llm.call_tool(system=READ_SYSTEM, tool_name="record_document",
                              description="Record the billing facts in this document.",
                              schema=READ_SCHEMA,
                              content=[llm.document_block(name, content, getattr(upload, "content_type", "")),
                                       {"type": "text", "text": f"Record the billing facts in {name}."}])
        draft.data.setdefault("sources", []).append({
            "file": name, "type": facts.get("document_type", "other"), "summary": facts.get("summary", ""),
            "facts": facts})
        notes.append(f"Read {name}.")
    draft.save()
    return notes


# --- Proposing the invoice --------------------------------------------------------------------------------------

PROPOSE_SYSTEM = """You prepare a draft customer invoice for a person to review, from facts read from billing \
documents and the organisation's invoice set-up. Follow these rules.
- One line per chargeable item. Use the set-up's item names as line descriptions; add detail after a dash or in the \
customer's usual style (see past descriptions), e.g. "Day Labour on 01 September".
- Every quantity and unit price must come from the facts or from the item list's standard rate. Say where each \
came from in "source" (file and page, or "item list").
- State unit prices on the set-up's tax basis ({basis}). If a document states prices on the other basis, convert \
them and say so in "note".
- Where documents disagree (for example an agreement's estimate and a later order), follow the set-up's \
instructions; otherwise use the most specific and latest document, and ask a question naming both values.
- A chargeable item with no price found: include it with unit price 0 and ask a question. Never guess a price.
- Look for revenue that could be missed: terms that apply per ticket, as a percentage, as a minimum or on a \
condition. Ask about them in questions; only add a line when the documents show it applies.
- Deposits, payments and settlements are not invoice lines: list them in payments_to_apply.
- List every assumption you made in "assumptions" (e.g. which document you followed where they disagree, a tax conversion, a rate taken from the item list, something left out and why).
- Ask only questions whose answer could change this invoice, and include the set-up's standing questions when \
they are relevant. Keep each question short and specific.
- Don't choose account codes or tax rates; iLA codes each line itself.
- Invoice date: follow the set-up's instructions, otherwise leave it empty. Leave the invoice number empty unless \
the person gives one.
When revising, apply the person's request to the current draft, return the whole updated draft, and list each \
change in "changes". If the request is unclear or needs information you don't have, change nothing and ask."""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "header": {"type": "object", "properties": {
            "reference": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "number": {"type": "string"}}, "required": ["reference"]},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string", "description": "The set-up item this line is, or empty."},
            "description": {"type": "string"}, "quantity": {"type": "number"},
            "unit_amount": {"type": "number"}, "discount_percent": {"type": "number"},
            "source": {"type": "string"}, "note": {"type": "string"}},
            "required": ["description", "quantity", "unit_amount", "source"]}},
        "payments_to_apply": {"type": "array", "items": {"type": "object", "properties": {
            "description": {"type": "string"}, "amount": {"type": "number"}, "source": {"type": "string"}},
            "required": ["description", "amount"]}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["header", "lines", "questions"],
}


def _setup_brief(setup):
    items = [{"name": i.name, "aliases": i.aliases, "unit": i.get_unit_display(),
              "rate": str(i.rate) if i.rate is not None else "", "markup_percent": str(i.markup_percent or ""),
              "notes": i.notes} for i in setup.items.all()]
    return {"name": setup.name, "tax_basis": setup.get_tax_basis_display(), "due_days": setup.due_days,
            "reference_pattern": setup.reference_pattern, "sources": setup.sources,
            "standing_questions": setup.questions, "instructions": setup.instructions, "items": items}


def _compose(ctx, draft, instruction="", intro="", request=""):
    import json

    setup = draft.setup
    customer = draft.data.get("customer") or {}
    past = [li["description"] for inv in _recent(ctx)
            if inv["contact_id"] == customer.get("id") or inv["contact"] == customer.get("name")
            for li in inv["lines"] if li["description"]][:30]
    context = {
        "today": ctx.today.isoformat(), "customer": customer.get("name", ""),
        "event_or_job": (draft.data.get("event") or {}).get("name", ""), "setup": _setup_brief(setup),
        "past_line_descriptions_for_this_customer": past,
        "facts_from_documents": [{"file": s["file"], **s["facts"]} for s in draft.data.get("sources", [])],
    }
    if instruction and draft.data.get("lines") is not None:
        context["current_draft"] = _draft_for_model(draft)
    task = (f"Revise the draft. The person says: {instruction}" if instruction and "current_draft" in context
            else "Prepare the draft invoice." + (f" The person says: {request}" if request else ""))
    proposal = llm.call_tool(
        system=PROPOSE_SYSTEM.format(basis=setup.get_tax_basis_display()), tool_name="propose_invoice",
        description="Return the draft invoice.", schema=PROPOSE_SCHEMA,
        content=[{"type": "text", "text": json.dumps(context, default=str)}, {"type": "text", "text": task}])
    before = [dict(li) for li in draft.data.get("lines", [])]
    _apply(ctx, draft, proposal)
    changes = proposal.get("changes") or (_diff(before, draft.data["lines"]) if before else [])
    draft.history = draft.history + [{"at": ctx.today.isoformat(), "said": instruction or "(first draft)",
                                      "changes": changes}]
    draft.step = InvoiceDraft.Step.REVIEW
    draft.save()
    return show(ctx, draft, intro=intro, changes=changes if instruction else [])


def _draft_for_model(draft):
    return {"header": {k: draft.data.get(k, "") for k in ("reference", "date", "due_date", "number")},
            "lines": [{k: li.get(k) for k in ("item", "description", "quantity", "unit_amount", "discount_percent",
                                              "source", "note")} for li in draft.data.get("lines", [])],
            "payments_to_apply": draft.data.get("payments", []), "assumptions": draft.data.get("assumptions", []),
            "questions": draft.data.get("questions", [])}


def _number(value, default="0"):
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except InvalidOperation:
        return Decimal(default)


def _iso_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _apply(ctx, draft, proposal):
    """Turn the model's proposal into the draft: code each line, calculate amounts and tax, and flag problems."""
    setup = draft.setup
    header = proposal.get("header") or {}
    data = draft.data
    data["reference"] = header.get("reference") or data.get("reference", "")
    data["number"] = header.get("number") or data.get("number") or _next_number(ctx)
    day = _iso_date(header.get("date")) or _iso_date(data.get("date")) or ctx.today
    data["date"] = day.isoformat()
    due = _iso_date(header.get("due_date"))
    if due is None or (header.get("date") and not header.get("due_date")):
        due = day + timedelta(days=setup.due_days)
    data["due_date"] = due.isoformat()

    items = {i.name.lower(): i for i in setup.items.all()}
    for item in list(items.values()):
        for alias in filter(None, (a.strip().lower() for a in item.aliases.split(","))):
            items.setdefault(alias, item)
    history = _coding_history(ctx)
    tax_rates = _tax_rates(ctx)
    tracking = (data.get("event") or {}).get("tracking_option", "")
    lines = []
    for raw in proposal.get("lines") or []:
        line = {"item": raw.get("item", ""), "description": (raw.get("description") or "").strip(),
                "quantity": str(_number(raw.get("quantity"), "1").normalize()),
                "unit_amount": str(_number(raw.get("unit_amount")).quantize(CENT)),
                "discount_percent": str(_number(raw.get("discount_percent")).normalize()),
                "source": raw.get("source", ""), "note": raw.get("note", ""), "checks": []}
        item = items.get(line["item"].lower()) or _closest(items, line["description"])
        line["item"] = item.name if item else ""
        coded = history.get(_key(line["description"])) or _closest_value(history, line["description"])
        # Most specific first: the item list, then how the same line was coded before, then the set-up default.
        if item and item.account_code:
            line["account_code"], line["coded_from"] = item.account_code, "item list"
        elif coded:
            line["account_code"], line["coded_from"] = coded["account"], f"as on {coded['number']}"
        else:
            line["account_code"] = setup.default_account_code
            line["coded_from"] = "set-up default" if setup.default_account_code else ""
        line["tax_type"] = (item and item.tax_type) or (coded or {}).get("tax_name", "") or setup.default_tax_type
        line["tracking_name"] = setup.tracking_name if tracking else ""
        line["tracking_option"] = tracking
        qty, unit, disc = _number(line["quantity"]), _number(line["unit_amount"]), _number(line["discount_percent"])
        amount = (qty * unit * (1 - disc / 100)).quantize(CENT, ROUND_HALF_UP)
        line["amount"] = str(amount)
        rate = (tax_rates.get(line["tax_type"]) or {}).get("rate")
        if rate is None:
            line["tax"] = "0.00"
        elif setup.tax_basis == LineAmountType.INCLUSIVE:
            line["tax"] = str((amount * rate / (100 + rate)).quantize(CENT, ROUND_HALF_UP))
        else:
            line["tax"] = str((amount * rate / 100).quantize(CENT, ROUND_HALF_UP))
        if unit == 0:
            line["checks"].append("$0 line")
        if not item:
            line["checks"].append("not in the item list")
        elif item.rate is not None and item.unit != item.Unit.AT_COST and unit != item.rate and disc == 0:
            line["checks"].append(f"item list rate is {item.rate}")
        if not line["account_code"]:
            line["checks"].append("no account code")
        if not line["tax_type"]:
            line["checks"].append("no tax rate")
        elif line["tax_type"] not in tax_rates:
            line["checks"].append(f'tax rate "{line["tax_type"]}" not found in Xero')
        lines.append(line)
    data["lines"] = lines
    data["payments"] = [{"description": p.get("description", ""), "amount": str(_number(p.get("amount")).quantize(CENT)),
                         "source": p.get("source", "")} for p in proposal.get("payments_to_apply") or []]
    data["questions"] = list(proposal.get("questions") or [])
    data["assumptions"] = list(proposal.get("assumptions") or [])
    data["choices"] = []


def _totals(draft):
    lines = draft.data.get("lines", [])
    amount = sum((_number(li["amount"]) for li in lines), Decimal("0"))
    tax = sum((_number(li["tax"]) for li in lines), Decimal("0"))
    if draft.setup.tax_basis == LineAmountType.INCLUSIVE:
        return amount - tax, tax, amount
    return amount, tax, amount + tax


def _diff(before, after):
    old = {li["description"]: li for li in before}
    new = {li["description"]: li for li in after}
    changes = [f"Added {d}" for d in new if d not in old] + [f"Removed {d}" for d in old if d not in new]
    for d in new.keys() & old.keys():
        for field, label in (("quantity", "quantity"), ("unit_amount", "price"), ("discount_percent", "discount")):
            if _number(new[d][field]) != _number(old[d][field]):
                changes.append(f"{d}: {label} {old[d][field]} → {new[d][field]}")
    return changes


# --- Xero look-ups (read-only) ----------------------------------------------------------------------------------

def _recent(ctx):
    if not hasattr(ctx, "_recent_sales"):
        ctx._recent_sales = ctx.source.recent_sales_invoices()
    return ctx._recent_sales


def _tax_rates(ctx):
    if not hasattr(ctx, "_tax_rates"):
        ctx._tax_rates = ctx.source.tax_rates()
    return ctx._tax_rates


def _tracking_options(ctx, draft):
    category = draft.setup.tracking_name
    if not category:
        return []
    return ctx.source.tracking_categories().get(category, [])


def _key(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _similarity(a, b):
    a, b = _key(a), _key(b)
    if a and a in b:
        return 1.0 if a == b else 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _closest(items, description, cutoff=0.85):
    best = max(items, key=lambda name: _similarity(name, description.split(" - ")[0]), default=None)
    if best and _similarity(best, description.split(" - ")[0]) >= cutoff:
        return items[best]
    return None


def _closest_value(history, description, cutoff=0.8):
    best = max(history, key=lambda k: difflib.SequenceMatcher(None, k, _key(description)).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, best, _key(description)).ratio() >= cutoff:
        return history[best]
    return None


def _coding_history(ctx):
    """How recent invoice lines were coded, by description: {description key: {account, tax_name, number}}."""
    names = {v["type"]: name for name, v in _tax_rates(ctx).items()}
    history = {}
    for inv in _recent(ctx):
        for li in inv["lines"]:
            key = _key(li["description"])
            if key and key not in history and li["account"]:
                history[key] = {"account": li["account"], "tax_name": names.get(li["tax_type"], ""),
                                "number": inv["number"]}
    return history


def _next_number(ctx):
    """Suggest the next number in the organisation's most used invoice numbering (e.g. INV-2189)."""
    best = {}
    for inv in _recent(ctx):
        m = re.fullmatch(r"(.*?)(\d+)", inv["number"] or "")
        if m:
            prefix, digits = m.groups()
            count, top, width = best.get(prefix, (0, 0, 0))
            best[prefix] = (count + 1, max(top, int(digits)), max(width, len(digits)))
    if not best:
        return ""
    prefix, (_, top, width) = max(best.items(), key=lambda kv: kv[1][0])
    return f"{prefix}{top + 1:0{width}d}"


# --- Approval ---------------------------------------------------------------------------------------------------

def _approve(ctx, draft):
    data, setup = draft.data, draft.setup
    errors, warnings = [], []
    if not data.get("lines"):
        errors.append("The invoice has no lines.")
    if not data.get("number"):
        errors.append('There\'s no invoice number. Tell me one, e.g. "number INV-2189".')
    elif ctx.source.invoice_number_used(data["number"]):
        errors.append(f"{data['number']} is already used in Xero. Tell me another number.")
    codes = {a["code"] for a in ctx.source.accounts().values() if a["code"] and a["status"] == "ACTIVE"}
    tax_rates = _tax_rates(ctx)
    tracking = ctx.source.tracking_categories() if any(li.get("tracking_option") for li in data["lines"]) else {}
    for n, li in enumerate(data.get("lines", []), 1):
        if li["account_code"] not in codes:
            errors.append(f"Line {n} ({li['description']}): account {li['account_code'] or '(none)'} isn't an active "
                          "account in Xero.")
        rate = tax_rates.get(li["tax_type"])
        if not rate or not rate["active"]:
            errors.append(f"Line {n} ({li['description']}): tax rate {li['tax_type'] or '(none)'} isn't active in "
                          "Xero.")
        if li.get("tracking_option") and li["tracking_option"] not in tracking.get(li["tracking_name"], []):
            errors.append(f"Line {n}: {li['tracking_name']} option {li['tracking_option']} isn't in Xero.")
        if _number(li["unit_amount"]) == 0:
            warnings.append(f"Line {n} ({li['description']}) is $0.")
    customer = data.get("customer") or {}
    if customer.get("new"):
        warnings.append(f"{customer['name']} isn't a contact in Xero yet; the import will create it.")
    if errors:
        return show(ctx, draft, intro="Not approved yet. Fix these first (tell me in plain English):",
                    problems=errors)

    invoice = {
        "contact_name": customer.get("name", ""), "email": customer.get("email", ""), "number": data["number"],
        "reference": data.get("reference", ""), "date": date.fromisoformat(data["date"]),
        "due_date": date.fromisoformat(data["due_date"]), "branding_theme": setup.branding_theme,
        "lines": [{"description": li["description"], "quantity": _number(li["quantity"]),
                   "unit_amount": _number(li["unit_amount"]),
                   "discount": _number(li["discount_percent"]) or None, "account_code": li["account_code"],
                   "tax_type": li["tax_type"], "tracking_name1": li.get("tracking_name", ""),
                   "tracking_option1": li.get("tracking_option", "")} for li in data["lines"]],
    }
    try:
        content = build_csv(setup.import_format, [invoice], date_format=setup.date_format)
    except InvoiceFileError as exc:
        return show(ctx, draft, intro="Not approved yet:", problems=[str(exc)])
    subtotal, tax, total = _totals(draft)
    import_file = ImportFile.objects.create(
        organisation=ctx.organisation, target_system=setup.system, profile=f"{setup.name} · {setup.import_format}",
        file_name=f"{data['number']} {customer.get('name', '')}.csv".strip(), row_count=len(data["lines"]),
        content=content, content_type="text/csv", invoice_setup=setup, built_by=ctx.user,
        validation={"warnings": warnings, "draft": draft.pk, "total": str(total), "tax": str(tax)})
    import_file.stamp(ctx.user, content)
    draft.status, draft.import_file = InvoiceDraft.Status.APPROVED, import_file
    draft.save()
    AuditLog.objects.create(
        organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
        input=f"approve invoice {data['number']}", target_type="core.importfile", target_id=str(import_file.pk),
        result={"customer": customer.get("name", ""), "total": str(total), "sha256": import_file.sha256,
                "lines": len(data["lines"])})

    result = Result(f"Approved: {data['number']} for {customer.get('name', '')}",
                    subtitle=f"Import file saved under Import files ({import_file.file_name}).")
    result.add(Figures([Figure("Subtotal", subtotal), Figure(_tax_label(draft), tax), Figure("Total", total)]))
    basis = "Tax inclusive" if setup.tax_basis == LineAmountType.INCLUSIVE else "Tax exclusive"
    result.add(Note(f"In Xero: Sales → Invoices → Import, choose this file and select amounts as {basis}. It "
                    "imports as a draft for the usual approval.", "warning"))
    for w in warnings:
        result.add(Note(w, "warning"))
    result.link = reverse("console:file_download", args=[import_file.pk])
    result.link_text = f"Download {import_file.file_name}"
    return result


# --- Display ----------------------------------------------------------------------------------------------------

def _customer_name(draft):
    return (draft.data.get("customer") or {}).get("name", "")


def _tax_label(draft):
    return "GST included" if draft.setup.tax_basis == LineAmountType.INCLUSIVE else "GST"


PROMPTS = {
    InvoiceDraft.Step.SETUP: "Which invoice set-up? Type a number.",
    InvoiceDraft.Step.CUSTOMER: "Who is the customer? Type part of their name and I'll search Xero.",
    InvoiceDraft.Step.EVENT: "Which event or job is this for? Pick a number or type its name.",
    InvoiceDraft.Step.REVIEW: "Type changes in plain English (e.g. \"security 24 hours\", \"remove catering\", "
                              "\"number ILA-TEST-2070\"), add files with <<, type approve to build the import file, "
                              "or cancel.",
}


def show(ctx, draft, *, intro="", changes=(), problems=()):
    setup = draft.setup
    title = "/_createInvoice"
    if setup:
        title += f" · {setup.name}"
    result = Result(title, subtitle=f"Step: {draft.get_step_display()}")
    if intro:
        result.add(Note(intro))
    for p in problems:
        result.add(Note(p, "warning"))
    header = [("Customer", _customer_name(draft) + (" (new)" if (draft.data.get("customer") or {}).get("new") else "")),
              ("Event or job", (draft.data.get("event") or {}).get("name", ""))]
    if any(v for _, v in header):
        table = result.add(Table([Column(""), Column("")]))
        for label, value in header:
            if value:
                table.add(label, value)

    if draft.step == InvoiceDraft.Step.SOURCES or draft.data.get("sources"):
        sources = draft.data.get("sources", [])
        if sources:
            table = result.add(Table([Column("File"), Column("What it is"), Column("Summary")],
                                     title="Billing information"))
            for s in sources:
                table.add(s["file"], s["type"].replace("_", " "), s["summary"])
    if draft.step == InvoiceDraft.Step.SOURCES:
        wanted = [w.strip() for w in (setup.sources or "").splitlines() if w.strip()]
        ask = "Where's the billing information?"
        if wanted:
            ask += " This set-up uses: " + "; ".join(wanted) + "."
        ask += (" Choose the files with << (you can select several), or type the details. When you have given "
                "everything, type done (or go ahead) and I'll draft the invoice.")
        if not llm.available():
            ask += " Note: reading files needs the language model, which isn't set up on this server yet."
        result.add(Note(ask, "warning"))

    if draft.step == InvoiceDraft.Step.REVIEW:
        _show_invoice(result, draft, changes)

    choices = draft.data.get("choices") or []
    if choices and draft.step in (InvoiceDraft.Step.SETUP, InvoiceDraft.Step.CUSTOMER, InvoiceDraft.Step.EVENT):
        table = result.add(Table([Column("#", NUMBER), Column("Choice")], title="Pick one"))
        for n, c in enumerate(choices, 1):
            table.add(n, c["label"])
    if draft.step in PROMPTS:
        result.add(Note(PROMPTS[draft.step], "warning"))
    return result


def _show_invoice(result, draft, changes):
    data = draft.data
    subtotal, tax, total = _totals(draft)
    result.add(Figures([
        Figure("Invoice number", data.get("number") or "–", TEXT),
        Figure("Date", f"{_fmt_day(data.get('date'))} · due {_fmt_day(data.get('due_date'))}", TEXT),
        Figure("Reference", data.get("reference") or "–", TEXT),
        Figure(f"Total ({'GST inclusive' if draft.setup.tax_basis == LineAmountType.INCLUSIVE else 'plus GST'})",
               total, detail=f"Subtotal {subtotal:,.2f} · {_tax_label(draft)} {tax:,.2f}"),
    ]))
    if changes:
        result.add(Note("Changed: " + "; ".join(changes)))
    table = result.add(Table([Column("#"), Column("Description"), Column("Qty"), Column("Unit price", MONEY),
                              Column("Disc %"), Column("Amount", MONEY), Column("Account"), Column("Tax rate"),
                              Column("From"), Column("Check")], title="Lines"))
    for n, li in enumerate(data.get("lines", []), 1):
        disc = li.get("discount_percent", "0")
        table.add(n, li["description"], li["quantity"], _number(li["unit_amount"]),
                  "" if _number(disc) == 0 else disc, _number(li["amount"]),
                  li["account_code"] + (f" ({li['coded_from']})" if li.get("coded_from") else ""),
                  li["tax_type"], li["source"] + (f" · {li['note']}" if li.get("note") else ""),
                  ", ".join(li.get("checks", [])))
    table.add("", "Subtotal", "", None, "", subtotal, "", "", "", "", total=True)
    table.add("", _tax_label(draft), "", None, "", tax, "", "", "", "", total=True)
    table.add("", "Total", "", None, "", total, "", "", "", "", total=True)
    payments = data.get("payments", [])
    if payments:
        paid = result.add(Table([Column("Payment or deduction"), Column("Amount", MONEY), Column("From")],
                                title="Payments to apply in Xero (not invoice lines)"))
        applied = Decimal("0")
        for p in payments:
            paid.add(p["description"], _number(p["amount"]), p.get("source", ""))
            applied += _number(p["amount"])
        paid.add("Amount due after these", total - applied, "", total=True)
    for a in data.get("assumptions", []):
        result.add(Note("Assumed: " + a))
    for q in data.get("questions", []):
        result.add(Note("Question: " + q, "warning"))


def _fmt_day(value):
    day = _iso_date(value)
    return day.strftime("%d %b %Y").lstrip("0") if day else "–"
