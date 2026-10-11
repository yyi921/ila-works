"""The builder: templates superusers start from, and running the commands they build.

A built command is called ``/_name`` (or just ``_name``). Native iLA commands never start with "_", so a built
command can never hide or replace one. "Start from scratch" commands (shortcuts) run native commands in order and
show the results as one report; ``{text}`` and ``{params}`` in a step take whatever the user typed after the name.
"""

import re
from dataclasses import dataclass

from core.models import Organisation
from xero.api import XeroAPIError

from .models import CustomCommand
from .output import Note, Result, Table
from .parser import CommandSyntaxError, parse
from .periods import PeriodError
from .registry import CommandError, lookup

MAX_STEPS = 10
NAME_RE = re.compile(r"^_[A-Za-z][A-Za-z0-9_-]{1,30}$")
PLACEHOLDER_RE = re.compile(r"\{(\w*)\}")
PLACEHOLDERS = {"text", "params"}
# Commands that change what the session points at or only make sense typed by hand.
NOT_IN_STEPS = {"use", "connect", "clean"}
STEP_ERRORS = (CommandSyntaxError, CommandError, XeroAPIError, PeriodError)


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    summary: str
    goals: tuple
    available: bool
    status_note: str = ""


TEMPLATES = (
    Template(
        CustomCommand.Template.SHORTCUT, "Start from scratch",
        "Run one or more iLA commands in order under a name of your own, e.g. a month-end pack or a supplier check "
        "you run every week.",
        ("Up to 10 iLA commands, run in order and shown as one report",
         "{text} and {params} pass on what you type after the command's name",
         ">> file.xlsx saves every table in the report"),
        available=True,
    ),
    Template(
        CustomCommand.Template.CREATE_INVOICE, "createInvoice",
        "Build a customer invoice as an import file from the information you feed it: the agreement, orders, "
        "supplier bills and settlements. Works for any business: what counts as agreed and incurred, and how items "
        "are priced, is set up per organisation.",
        ("Lines priced from what was agreed and what was incurred",
         "Every line carries the details needed later, e.g. event tag, revenue account, tax code",
         "Checked against the organisation's accounts, tax rates and tracking before the file is written",
         "Questions to answer before the invoice is approved",
         "Saved as an import file for Xero (imports as a draft); other systems later"),
        available=True,
    ),
)


def template(key):
    return next((t for t in TEMPLATES if t.key == key), None)


def can_build(user):
    return user.is_authenticated and (user.is_superuser or user.has_perm("console.build_commands"))


def clean_name(value):
    name = value.strip().lstrip("/")
    if name and not name.startswith("_"):
        name = "_" + name
    if not NAME_RE.match(name):
        raise ValueError('Use 2 to 31 letters, digits, "-" or "_" after the "_", starting with a letter, '
                         "e.g. _weekly.")
    return name


def clean_steps(text):
    """The steps as a list of command lines, or ValueError whose ``args`` are every problem found."""
    steps = [line.strip() for line in text.splitlines() if line.strip()]
    if not steps:
        raise ValueError("Add at least one command, e.g. /status.")
    problems = []
    if len(steps) > MAX_STEPS:
        problems.append(f"Use at most {MAX_STEPS} commands.")
    for number, step in enumerate(steps, 1):
        unknown = set(PLACEHOLDER_RE.findall(step)) - PLACEHOLDERS
        if unknown:
            problems.append(f"Line {number}: {{{unknown.pop()}}} isn't a placeholder; use {{text}} or {{params}}.")
            continue
        try:
            parsed = parse(fill(step, "x", ["x"]))
        except CommandSyntaxError as exc:
            problems.append(f"Line {number}: {exc}")
            continue
        spec = lookup(parsed.name)
        if parsed.name.startswith("_"):
            problems.append(f"Line {number}: built commands can't run other built commands.")
        elif spec is None:
            problems.append(f"Line {number}: there's no /{parsed.name} command.")
        elif spec.name in NOT_IN_STEPS:
            problems.append(f"Line {number}: /{spec.name} can't be used inside a built command.")
        elif parsed.input_file or parsed.output_file:
            problems.append(f"Line {number}: leave out << and >>; add >> when you run the command instead.")
    if problems:
        raise ValueError(*problems)
    return steps


def fill(step, text, params):
    return (step.replace("{text}", text)
                .replace("{params}", " ".join("--" + p for p in params))
                .strip())


def find(organisation, name):
    if organisation is None:
        return None
    return CustomCommand.objects.filter(organisation=organisation, name__iexact=name, is_active=True).first()


def active_commands(organisation):
    """The organisation's commands anyone can run (none for an organisation that isn't saved yet)."""
    if not isinstance(organisation, Organisation):
        return []
    return list(CustomCommand.objects.filter(organisation=organisation, is_active=True))


def run(ctx, cmd, custom):
    if cmd.input_file:
        raise CommandError(f"/{custom.name} doesn't take an input file (<<).")
    joined = "\n".join(custom.steps)
    if cmd.text and "{text}" not in joined:
        raise CommandError(f'/{custom.name} doesn\'t take text after its name. Type /help {custom.name} to see it.')
    if cmd.params and "{params}" not in joined:
        raise CommandError(f"/{custom.name} doesn't take -- parameters. Type /help {custom.name} to see it.")
    if "{text}" in joined and not cmd.text:
        raise CommandError(f"/{custom.name} needs text after its name, e.g. /{custom.name} <name or number>.")

    result = Result(f"/{custom.name}", subtitle=custom.summary)
    for step in custom.steps:
        line = fill(step, cmd.text, cmd.params)
        try:
            parsed = parse(line)
            spec = lookup(parsed.name)
            if spec is None or spec.name in NOT_IN_STEPS:
                raise CommandError(f"/{parsed.name} isn't available inside a built command.")
            step_result = spec.func(ctx, parsed)
        except STEP_ERRORS as exc:
            result.add(Section(line, "Couldn't run"))
            result.add(Note(str(exc), "warning"))
            continue
        result.add(Section(line, step_result.title, step_result.subtitle))
        for block in step_result.blocks:
            if isinstance(block, Table) and not block.title:
                block.title = step_result.title
            result.add(block)
    return result


@dataclass
class Section:
    """Heading for one step's output inside a built command's report."""
    command: str
    title: str
    subtitle: str = ""

    def to_json(self):
        return {"type": "section", "command": self.command, "title": self.title, "subtitle": self.subtitle}
