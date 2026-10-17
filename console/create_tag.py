"""/createTag: create an iLA dimension from a CSV of its values, either brand new or replacing a Xero tracking
category, or refresh the values of one that exists.

Steps: what to create (replace a Xero tracking category, update an iLA dimension, or a new one) → its name → a CSV
of values (Code, Name, Description, optional Active and any extra columns, e.g. Venue or Event date) → review →
approve.

Replacing a Xero category never changes Xero. Each Xero option (e.g. CCH1221) becomes a value whose Code is the
option, linked to it, so tags still tie back to Xero while iLA shows the Name, Description and details from the
file. Nothing is saved to the dimension until the user approves.
"""

import csv
import io
import re
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.text import slugify

from core.models import AuditLog, Dimension, DimensionValue, SourceSystem, TrackingCategory, TrackingOption
from xero.api import XeroAPIError

from .models import TagDraft
from .output import NUMBER, TEXT, Column, Figure, Figures, Note, Result, Table
from .registry import CommandError

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
APPROVE_WORDS = {"approve", "approved", "ok approve", "yes approve"}
CANCEL_WORDS = {"cancel", "/cancel", "stop", "quit"}
KEEP_WORDS = {"ok", "okay", "yes", "keep", "keep it", "same"}
BASE_COLUMNS = {"code", "name", "description", "active"}
TRUE_WORDS = {"yes", "y", "true", "1", "active"}
FALSE_WORDS = {"no", "n", "false", "0", "archived", "inactive"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")
TITLE = "/createTag"


# --- Entry points -----------------------------------------------------------------------------------------------

def open_draft(ctx):
    if ctx.organisation is None:
        return None
    return TagDraft.objects.filter(organisation=ctx.organisation, user=ctx.user, status=TagDraft.Status.OPEN).first()


def start(ctx, cmd, params):
    """``/createTag [<name>] [--new] [<< values.csv]``: resume the open draft, or start one. With a name that
    matches and a file, the suggested name is kept and the file is read straight away."""
    if ctx.organisation is None:
        raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
    existing = open_draft(ctx)
    upload = ctx.input_file if cmd.input_file else None
    if existing and not cmd.text and "new" not in params.flags:
        if upload is not None:
            return answer(ctx, existing, "", [upload])
        return show(ctx, existing, intro="Picking up where you left off. Type cancel to drop it, or start again "
                                         "with /createTag --new.")
    if existing:
        existing.status = TagDraft.Status.CANCELLED
        existing.save()
    categories, problem = _categories(ctx)
    draft = TagDraft.objects.create(organisation=ctx.organisation, user=ctx.user, data={"hint": cmd.text.strip()})
    draft.data["choices"] = _target_choices(ctx, categories, draft.data["hint"])
    if draft.data["hint"]:
        match = _match_choice(draft.data["choices"], draft.data["hint"])
        if match:
            result = _set_target(ctx, draft, match["value"])
            if upload is None:
                return result
            return _set_name(ctx, draft, "", files=[upload])
    draft.save()
    intro = f"Xero tracking categories couldn't be read: {problem}" if problem else ""
    if upload is not None:
        intro += (" " if intro else "") + ("Pick what you're setting up first; I'll ask for the file after that. "
                                           "Your file wasn't read.")
    return show(ctx, draft, intro=intro)


def cancel(ctx):
    draft = open_draft(ctx)
    if draft is None:
        return None
    draft.status = TagDraft.Status.CANCELLED
    draft.save()
    return Result("Tag set-up cancelled", subtitle=f"{draft.data.get('name') or 'The new dimension'} was dropped. "
                                                   "Nothing was saved.")


def answer(ctx, draft, text, files=()):
    """Plain input (no "/") while a draft is open: an answer to the current question."""
    text = (text or "").strip()
    if text.startswith("<<"):
        text = ""  # "<< values.csv" only names the file being uploaded
    if text.lower() in CANCEL_WORDS:
        return cancel(ctx)
    if files and draft.step in (TagDraft.Step.FILE, TagDraft.Step.REVIEW):
        return _read_file(ctx, draft, files)
    if files:
        return show(ctx, draft, intro="Answer this question first; I'll ask for the file next.")
    if draft.step == TagDraft.Step.TARGET:
        choice = _pick(draft, text)
        if choice is None:
            return show(ctx, draft, intro="Type the number of your choice.")
        return _set_target(ctx, draft, choice["value"])
    if draft.step == TagDraft.Step.NAME:
        return _set_name(ctx, draft, text)
    if draft.step == TagDraft.Step.FILE:
        if text.lower() in APPROVE_WORDS:
            return show(ctx, draft, intro="Nothing to approve yet: choose a file without problems first.")
        return show(ctx, draft, intro="Choose the CSV file with << (e.g. << events.csv).")
    if text.lower() in APPROVE_WORDS:
        return _approve(ctx, draft)
    return show(ctx, draft, intro="Type approve to save it, choose a corrected file with <<, or type cancel.")


# --- Steps ------------------------------------------------------------------------------------------------------

def _categories(ctx):
    try:
        return ctx.source.tracking_category_list(), ""
    except (XeroAPIError, CommandError) as exc:
        return [], str(exc)


def _target_choices(ctx, categories, hint):
    dimensions = list(Dimension.objects.filter(organisation=ctx.organisation).select_related("tracking_category")
                      .order_by("name"))
    by_category = {d.tracking_category.name: d for d in dimensions if d.tracking_category_id}
    by_category.update({d.source_name: d for d in dimensions if d.replaces_source and d.source_name})
    choices, linked = [], set()
    for c in sorted(categories, key=lambda c: (c["status"] != "Active", c["name"].lower())):
        if c["status"] == "Deleted":
            continue
        active = sum(o["status"] == "Active" for o in c["options"])
        dim = by_category.get(c["name"])
        label = f"Replace Xero's {c['name']} tracking ({active} active options"
        label += ", archived)" if c["status"] != "Active" else ")"
        if dim:
            label += f" · updates iLA dimension {dim.name}"
            linked.add(dim.pk)
        choices.append({"label": label, "match": [c["name"]] + ([dim.name, dim.code] if dim else []),
                        "value": {"kind": "replace", "category": c, "dimension": dim.pk if dim else None}})
    for d in dimensions:
        if d.pk not in linked:
            choices.append({"label": f"Update iLA dimension {d.name} ({d.values.count()} values)",
                            "match": [d.name, d.code], "value": {"kind": "update", "dimension": d.pk}})
    choices.append({"label": f'A new dimension "{hint}" (not in Xero)' if hint else "A new dimension (not in Xero)",
                    "match": [], "value": {"kind": "new"}})
    return choices


def _match_choice(choices, text):
    wanted = text.strip().lower()
    return next((c for c in choices if any(wanted == m.lower() for m in c["match"])), None)


def _pick(draft, text):
    choices = draft.data.get("choices") or []
    if text.isdigit() and 1 <= int(text) <= len(choices):
        return choices[int(text) - 1]
    return _match_choice(choices, text) if text else None


def _set_target(ctx, draft, target):
    draft.data["target"] = target
    draft.data["choices"] = []
    draft.dimension = (Dimension.objects.filter(organisation=ctx.organisation, pk=target.get("dimension")).first()
                       if target.get("dimension") else None)
    if draft.dimension:
        draft.data["suggested"] = draft.dimension.name
    elif target["kind"] == "replace":
        draft.data["suggested"] = target["category"]["name"]
    else:
        draft.data["suggested"] = draft.data.get("hint", "")
    draft.step = TagDraft.Step.NAME
    draft.save()
    return show(ctx, draft)


def _set_name(ctx, draft, text, files=()):
    suggested = draft.data.get("suggested", "")
    name = suggested if (text.lower() in KEEP_WORDS or not text) and suggested else text
    if not name:
        return show(ctx, draft, intro="Type a name for the dimension, e.g. Event.")
    if len(name) > 100:
        return show(ctx, draft, intro="Keep the name to 100 characters or fewer.")
    clash = Dimension.objects.filter(organisation=ctx.organisation, name__iexact=name)
    if draft.dimension:
        clash = clash.exclude(pk=draft.dimension.pk)
    if clash.exists():
        return show(ctx, draft, intro=f'There\'s already a dimension called "{name}". Pick it with /createTag {name} '
                                      "to update it, or type another name.")
    draft.data["name"] = name
    draft.step = TagDraft.Step.FILE
    draft.save()
    if files:
        return _read_file(ctx, draft, files)
    return show(ctx, draft)


# --- Reading the file -------------------------------------------------------------------------------------------

def _read_file(ctx, draft, files):
    if len(files) > 1:
        return show(ctx, draft, intro="Choose one CSV file.")
    upload = files[0]
    if not upload.name.lower().endswith(".csv"):
        return show(ctx, draft, intro=f"{upload.name} isn't a CSV file. Save it as CSV (in Excel: File → Save As → "
                                      "CSV UTF-8) and choose it again.")
    if upload.size > MAX_FILE_BYTES:
        return show(ctx, draft, intro=f"{upload.name} is over 5 MB.")
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    parsed = parse_values(text, draft.data.get("target") or {}, draft.dimension)
    draft.data.update(parsed, file=upload.name)
    draft.step = TagDraft.Step.FILE if parsed["problems"] else TagDraft.Step.REVIEW
    draft.save()
    if parsed["problems"]:
        return show(ctx, draft, intro=f"{upload.name} needs fixing before it can be used. Correct it and choose it "
                                      "again with <<.")
    return show(ctx, draft, intro=f"Read {len(parsed['rows'])} values from {upload.name}.")


def _field_key(header):
    return slugify(header).replace("-", "_")[:50] or "field"


def parse_values(text, target, dimension=None):
    """Read the values CSV. Returns {"rows", "fields", "problems", "warnings"}: rows are {"code", "name",
    "description", "active", "attributes", "option"} and fields are the dimension's extra fields (existing ones kept,
    new columns added with a type inferred from their values)."""
    problems, warnings = [], []
    reader = csv.reader(io.StringIO(text))
    lines = [row for row in reader]
    while lines and not any(c.strip() for c in lines[0]):
        lines.pop(0)
    if not lines:
        return {"rows": [], "fields": [], "problems": ["The file is empty."], "warnings": []}
    headers = [h.strip() for h in lines[0]]
    index = {h.lower(): i for i, h in enumerate(headers) if h}
    for required in ("Code", "Name"):
        if required.lower() not in index:
            problems.append(f'The file needs a "{required}" column. The first row must be the column headings: '
                            "Code, Name, Description, …")
    if problems:
        return {"rows": [], "fields": [], "problems": problems, "warnings": []}

    existing_fields = {f["key"]: f for f in (dimension.attribute_fields if dimension else [])}
    extra = []  # (column index, field key, label)
    for i, h in enumerate(headers):
        if h and h.lower() not in BASE_COLUMNS:
            key = next((k for k, f in existing_fields.items() if (f.get("label") or k).lower() == h.lower()),
                       _field_key(h))
            if any(key == k for _, k, _ in extra):
                problems.append(f'The column "{h}" appears twice.')
            extra.append((i, key, h))

    data_rows = [(n, row) for n, row in enumerate(lines[1:], 2) if any(c.strip() for c in row)]
    if not data_rows:
        problems.append("The file has headings but no values below them.")
    if len(data_rows) > MAX_ROWS:
        problems.append(f"The file has {len(data_rows)} values; the most is {MAX_ROWS}.")
        data_rows = data_rows[:MAX_ROWS]

    def cell(row, column):
        i = index.get(column) if isinstance(column, str) else column
        return row[i].strip() if i is not None and i < len(row) else ""

    fields = []
    for i, key, label in extra:
        kind = existing_fields[key].get("type", "text") if key in existing_fields else _infer_type(
            [cell(row, i) for _, row in data_rows])
        fields.append({"key": key, "label": label, "type": kind,
                       "required": bool(existing_fields.get(key, {}).get("required"))})
    fields += [f for k, f in existing_fields.items() if k not in {f["key"] for f in fields}]

    options = {}
    if target.get("kind") == "replace":
        options = {o["name"].lower(): o for o in target["category"]["options"]}
    taken = {cell(row, "code").lower() for _, row in data_rows} | set(options)
    if dimension is not None:
        taken |= {v.code.lower() for v in dimension.values.all()}
    date_column = _date_column(fields, extra)
    rows, seen, made = [], {}, []
    for n, row in data_rows:
        code, name = cell(row, "code"), cell(row, "name")
        if not code and not name:
            problems.append(f"Row {n}: Code and Name are both empty.")
            continue
        if not code:
            code = make_code(name, _convert(cell(row, date_column), Dimension.AttributeType.DATE)
                             if date_column is not None else None, taken)
            taken.add(code.lower())
            made.append(code)
        if len(code) > 100:
            problems.append(f"Row {n}: Code {code[:20]}… is longer than 100 characters.")
            continue
        if code.lower() in seen:
            problems.append(f"Row {n}: Code {code} is also on row {seen[code.lower()]}.")
            continue
        seen[code.lower()] = n
        if not name:
            problems.append(f"Row {n} ({code}): Name is empty.")
        elif len(name) > 255:
            problems.append(f"Row {n} ({code}): Name is longer than 255 characters.")
        active_text = cell(row, "active").lower()
        if active_text and active_text not in TRUE_WORDS | FALSE_WORDS:
            problems.append(f"Row {n} ({code}): Active must be yes or no, not {cell(row, 'active')}.")
        attributes = {}
        for i, key, label in extra:
            value = cell(row, i)
            if not value:
                continue
            kind = next(f["type"] for f in fields if f["key"] == key)
            converted = _convert(value, kind)
            if converted is None:
                problems.append(f"Row {n} ({code}): {label} must be {_kind_words(kind)}, not {value}.")
            else:
                attributes[key] = converted
        for f in fields:
            if f.get("required") and f["key"] not in attributes:
                problems.append(f"Row {n} ({code}): {f['label']} is required.")
        option = options.get(code.lower())
        rows.append({"code": option["name"] if option else code, "name": name,
                     "description": cell(row, "description"), "active": active_text not in FALSE_WORDS,
                     "attributes": attributes, "option": option["name"] if option else ""})

    if made:
        linked = [c for c in made if c.lower() in options]
        warnings.append(f"{len(made)} row(s) had no Code, so iLA made one from the date and name: "
                        f"{_short_list(made)}." + (f" {_short_list(linked)} match Xero option(s) and will be linked "
                                                   "to them; check they're the right events." if linked else ""))
    if options:
        category = target["category"]["name"]
        not_in_xero = [r["code"] for r in rows if not r["option"]]
        if not_in_xero:
            warnings.append(f"{len(not_in_xero)} code(s) aren't {category} options in Xero, so they'll be iLA-only "
                            f"values: {_short_list(not_in_xero)}.")
        listed = {r["code"].lower() for r in rows}
        missing = [o["name"] for o in target["category"]["options"]
                   if o["name"].lower() not in listed and o["status"] == "Active"]
        if missing:
            warnings.append(f"{len(missing)} active {category} option(s) in Xero aren't in the file. They'll be kept "
                            f"with the Xero code as their name: {_short_list(missing)}. Add them to the file to name "
                            "them.")
    if dimension is not None:
        listed = {r["code"].lower() for r in rows}
        untouched = [v.code for v in dimension.values.all() if v.code.lower() not in listed]
        if untouched:
            warnings.append(f"{len(untouched)} value(s) already in {dimension.name} aren't in the file and stay as "
                            f"they are: {_short_list(untouched)}.")
    return {"rows": rows, "fields": fields, "problems": problems, "warnings": warnings}


def _date_column(fields, extra):
    """The column index of the date to build codes from: a date field whose name says start, else the first one."""
    dates = [(i, label) for i, key, label in extra
             if next(f["type"] for f in fields if f["key"] == key) == Dimension.AttributeType.DATE]
    starts = [i for i, label in dates if "start" in label.lower()]
    return (starts or [i for i, _ in dates] or [None])[0]


def make_code(name, iso_date, taken):
    """A readable code for a value with none: YYMMDD plus the first three letters of the name when there's a date
    (260805DEP, as TECC's event codes are made), else the name's letters (ACRRM, TOWNSVILLE). A number is added
    if the code is already used."""
    letters = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    base = (iso_date[2:].replace("-", "") + letters[:3]) if iso_date else letters[:10]
    base = base or "VALUE"
    code, n = base, 2
    while code.lower() in taken:
        code, n = f"{base}{n}", n + 1
    return code


def _short_list(items, limit=8):
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _infer_type(values):
    values = [v for v in values if v]
    if not values:
        return Dimension.AttributeType.TEXT
    for kind in (Dimension.AttributeType.BOOLEAN, Dimension.AttributeType.NUMBER, Dimension.AttributeType.DATE):
        if all(_convert(v, kind) is not None for v in values):
            return kind
    return Dimension.AttributeType.TEXT


def _convert(value, kind):
    """The cell as the field's type (dates as YYYY-MM-DD), or None if it isn't one."""
    if kind == Dimension.AttributeType.BOOLEAN:
        low = value.lower()
        return True if low in {"yes", "y", "true"} else False if low in {"no", "n", "false"} else None
    if kind == Dimension.AttributeType.NUMBER:
        cleaned = value.replace(",", "").replace("$", "")
        if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
            return None
        number = float(cleaned)
        return int(number) if number.is_integer() and "." not in cleaned else number
    if kind == Dimension.AttributeType.DATE:
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    return value


def _kind_words(kind):
    return {"number": "a number", "date": "a date (e.g. 30/06/2026)", "boolean": "yes or no"}.get(kind, "text")


def template_csv(draft):
    """A starting file for the draft: the Xero options to name when replacing a category, the current values when
    updating a dimension, or the headings and an example row for a new one."""
    out = io.StringIO()
    writer = csv.writer(out)
    dimension, target = draft.dimension, draft.data.get("target") or {}
    fields = list(dimension.attribute_fields) if dimension else []
    writer.writerow(["Code", "Name", "Description", "Active"] + [f.get("label") or f["key"] for f in fields])
    current = {v.code.lower(): v for v in dimension.values.all()} if dimension else {}
    written = set()

    def write(code, value, active):
        attrs = value.attributes if value else {}
        writer.writerow([code, value.name if value else "", value.description if value else "",
                         "yes" if active else "no"]
                        + [_template_cell(attrs.get(f["key"])) for f in fields])
        written.add(code.lower())

    if target.get("kind") == "replace":
        for o in sorted(target["category"]["options"], key=lambda o: (o["status"] != "Active", o["name"].lower())):
            if o["status"] == "Deleted":
                continue
            write(o["name"], current.get(o["name"].lower()), o["status"] == "Active")
    for code, v in sorted(current.items()):
        if code not in written:
            write(v.code, v, v.is_active)
    if not written:
        writer.writerow(["CN26", "Celtic Nights 2026", "Concert, main arena", "yes"])
    return "﻿" + out.getvalue()


def _display(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return _template_cell(value)


def _template_cell(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value).strftime("%d/%m/%Y")
    return "" if value is None else str(value)


# --- Saving -----------------------------------------------------------------------------------------------------

def _approve(ctx, draft):
    data, target = draft.data, draft.data.get("target") or {}
    if data.get("problems") or not data.get("rows"):
        return show(ctx, draft, intro="Nothing to approve yet: choose a file without problems first.")
    try:
        with transaction.atomic():
            dimension, counts = _save(ctx, draft, target, data)
    except ValidationError as exc:
        return show(ctx, draft, intro="Not saved: " + "; ".join(exc.messages))
    draft.dimension, draft.status = dimension, TagDraft.Status.APPROVED
    draft.save()
    AuditLog.objects.create(organisation=ctx.organisation, user=ctx.user, action=AuditLog.Action.APPROVAL,
                            input=f"createTag {dimension.name} from {data.get('file', '')}",
                            target_type="core.dimension", target_id=str(dimension.pk),
                            result={"replaces": dimension.source_name, **counts})

    result = Result(f"Saved: {dimension.name}",
                    subtitle=(f"Replaces Xero's {dimension.source_name} tracking in iLA. Xero hasn't changed."
                              if dimension.replaces_source else "An iLA dimension. Xero hasn't changed."))
    figures = [Figure("New values", str(counts["created"]), TEXT),
               Figure("Updated values", str(counts["updated"]), TEXT),
               Figure("Unchanged", str(counts["unchanged"]), TEXT)]
    if target.get("kind") == "replace":
        figures.append(Figure("Named by Xero code", str(counts["from_xero"]), TEXT,
                              detail="Active Xero options that weren't in the file"))
    result.add(Figures(figures))
    result.add(Note(f"Type /dim {dimension.name} to see its values. To change them later, run /createTag "
                    f"{dimension.name} again with an updated file, or edit them on the admin page (Dimensions)."))
    return result


def _save(ctx, draft, target, data):
    org = ctx.organisation
    category, options = None, {}
    if target.get("kind") == "replace":
        category, options = _mirror_category(org, target["category"])
    dimension = draft.dimension
    if dimension is None:
        dimension = Dimension(organisation=org, code=_unique_code(org, data["name"]))
    dimension.name = data["name"]
    dimension.attribute_fields = data["fields"]
    if category is not None:
        dimension.tracking_category = category
        dimension.replaces_source = True
        dimension.source_name = category.name
    dimension.full_clean()
    dimension.save()

    existing = {v.code.lower(): v for v in dimension.values.all()}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "from_xero": 0}
    for row in data["rows"]:
        value = existing.get(row["code"].lower())
        before = _snapshot(value)
        if value is None:
            value = DimensionValue(organisation=org, dimension=dimension, code=row["code"])
        value.name, value.description, value.is_active = row["name"], row["description"], row["active"]
        value.attributes = {**(value.attributes or {}), **row["attributes"]}
        if row["option"]:
            value.tracking_option = options.get(row["option"].lower())
        value.full_clean()
        if before is None:
            counts["created"] += 1
        elif before != _snapshot(value):
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        value.save()
        existing[value.code.lower()] = value
    for key, option in options.items():
        if key not in existing and option.is_active:
            DimensionValue.objects.create(organisation=org, dimension=dimension, code=option.name, name=option.name,
                                          tracking_option=option)
            counts["from_xero"] += 1
        elif key in existing and existing[key].tracking_option_id is None:
            existing[key].tracking_option = option
            existing[key].save(update_fields=["tracking_option"])
    return dimension, counts


def _snapshot(value):
    if value is None or value.pk is None:
        return None
    return (value.name, value.description, value.is_active, value.attributes, value.tracking_option_id)


def _unique_code(org, name):
    base = slugify(name)[:45] or "dimension"
    code, n = base, 2
    while Dimension.objects.filter(organisation=org, code__iexact=code).exists():
        code, n = f"{base}-{n}", n + 1
    return code


def _mirror_category(org, category):
    """iLA's copy of a Xero tracking category and its options (matched on Xero's id, else on name), so values can
    link to them. Returns (TrackingCategory, {option name lower: TrackingOption})."""
    row = _find_mirror(TrackingCategory.objects.filter(organisation=org), category)
    if row is None:
        row = TrackingCategory(organisation=org, source_system=SourceSystem.XERO, source_id=category.get("id", ""))
    row.name, row.is_active = category["name"], category["status"] == "Active"
    row.save()
    options = {}
    for o in category["options"]:
        option = _find_mirror(TrackingOption.objects.filter(organisation=org, category=row), o)
        if option is None:
            option = TrackingOption(organisation=org, category=row, source_system=SourceSystem.XERO,
                                    source_id=o.get("id", ""))
        option.name, option.is_active = o["name"], o["status"] == "Active"
        option.save()
        options[o["name"].lower()] = option
    return row, options


def _find_mirror(queryset, item):
    if item.get("id"):
        found = queryset.filter(source_system=SourceSystem.XERO, source_id=item["id"]).first()
        if found:
            return found
    return queryset.filter(name__iexact=item["name"]).first()


# --- Display ----------------------------------------------------------------------------------------------------

FORMAT_ROWS = (
    ("Code", "Yes, or empty", "The short code. When replacing a Xero category, the Xero option exactly as in Xero (e.g. "
                    "CCH1221), so iLA can link them. Left empty, iLA makes one from the date and name (e.g. "
                    "261025FIR)."),
    ("Name", "Yes", "The name people should see, e.g. Celtic Nights 2026."),
    ("Description", "No", "Anything that helps people pick the right one."),
    ("Active", "No", "yes or no (blank means yes). No hides it from new tagging."),
    ("Any other column", "No", "Becomes an extra field, e.g. Venue, Event date (30/06/2026), Capacity, Ticketed "
                               "(yes/no). Its type is worked out from the values."),
)


def show(ctx, draft, *, intro=""):
    data, target = draft.data, draft.data.get("target") or {}
    result = Result(TITLE + (f" · {data['name']}" if data.get("name") else ""),
                    subtitle=f"Step: {draft.get_step_display()}")
    if intro:
        result.add(Note(intro))

    if draft.step == TagDraft.Step.TARGET:
        table = result.add(Table([Column("#", NUMBER), Column("Choice")], title="What are you setting up?"))
        for n, c in enumerate(data.get("choices") or [], 1):
            table.add(n, c["label"])
        result.add(Note("Replacing a Xero tracking category keeps Xero exactly as it is: iLA shows your names and "
                        "details (venue, dates…) instead of codes like CCH1221. A new dimension adds a way to tag "
                        "that Xero doesn't have, with no limit on how many. Type a number.", "warning"))
        return result

    if draft.step == TagDraft.Step.NAME:
        suggested = data.get("suggested")
        if target.get("kind") == "replace":
            category = target["category"]["name"]
            ask = (f"This replaces Xero's {category} tracking category in iLA. Xero isn't changed and its codes stay "
                   "linked; iLA shows the names and details from your file instead.")
        elif target.get("kind") == "update":
            ask = f"This updates the values of the iLA dimension {suggested}."
        else:
            ask = "This creates a brand-new iLA dimension. It isn't in Xero and never changes Xero."
        ask += (f' What should iLA call it? Type a name, or ok to keep "{suggested}".' if suggested
                else " What should iLA call it? e.g. Event, Venue, Campaign.")
        result.add(Note(ask, "warning"))
        return result

    _show_target(result, draft)
    for p in data.get("problems") or []:
        result.add(Note(p, "warning"))

    if draft.step == TagDraft.Step.FILE:
        table = result.add(Table([Column("Column"), Column("Required"), Column("What goes in it")],
                                 title="The values file (CSV)"))
        for row in FORMAT_ROWS:
            table.add(*row)
        result.link = reverse("console:createtag_template", args=[draft.pk])
        result.link_text = ("Download a starting file with the Xero codes filled in"
                            if target.get("kind") == "replace" or draft.dimension else "Download a starting file")
        result.add(Note("Fill in the file (Excel is fine; save it as CSV), then choose it with << (e.g. "
                        "<< events.csv).", "warning"))
        return result

    _show_rows(result, draft)
    for w in data.get("warnings") or []:
        result.add(Note(w))
    result.add(Note("Type approve to save it, choose a corrected file with <<, or type cancel. Nothing is saved "
                    "until you approve.", "warning"))
    return result


def _show_target(result, draft):
    data, target = draft.data, draft.data.get("target") or {}
    table = result.add(Table([Column(""), Column("")]))
    table.add("Dimension", data.get("name", ""))
    if target.get("kind") == "replace":
        table.add("Replaces", f"Xero tracking category {target['category']['name']}")
    table.add("Status", "Updates an existing iLA dimension" if draft.dimension else "New iLA dimension")
    if data.get("file"):
        table.add("File", data["file"])


def _show_rows(result, draft):
    data = draft.data
    fields = [f for f in data.get("fields", []) if any(f["key"] in r["attributes"] for r in data["rows"])]
    replace = (data.get("target") or {}).get("kind") == "replace"
    current = {v.code.lower(): v for v in draft.dimension.values.all()} if draft.dimension else {}
    columns = [Column("Code"), Column("Name"), Column("Description")]
    if replace:
        columns.append(Column("In Xero"))
    columns += [Column(f["label"]) for f in fields] + [Column("Active"), Column("Change")]
    table = result.add(Table(columns, title=f"Values ({len(data['rows'])})"))
    for r in data["rows"]:
        existing = current.get(r["code"].lower())
        if existing is None:
            change = "New"
        elif (existing.name, existing.description, existing.is_active) != (r["name"], r["description"], r["active"]) \
                or any(existing.attributes.get(k) != v for k, v in r["attributes"].items()):
            change = "Updated"
        else:
            change = "No change"
        cells = [r["code"], r["name"], r["description"]]
        if replace:
            cells.append("Yes" if r["option"] else "No")
        cells += [_display(r["attributes"].get(f["key"])) for f in fields]
        table.add(*cells, "Yes" if r["active"] else "No", change)
    new_fields = [f for f in data.get("fields", []) if f["key"] not in
                  {k["key"] for k in (draft.dimension.attribute_fields if draft.dimension else [])}]
    if new_fields:
        result.add(Note("New extra fields: " + ", ".join(f"{f['label']} ({_kind_words(f['type'])})"
                                                        for f in new_fields) + "."))
