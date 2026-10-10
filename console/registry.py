"""Command registry, run context and parameter reading."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from .periods import FinancialYear, PeriodError, parse_date, parse_period

COMMANDS = {}
ALIASES = {}


class CommandError(Exception):
    """Message is shown to the user as is."""


@dataclass
class CommandSpec:
    name: str
    func: object
    summary: str
    usage: str
    params: list  # (syntax, description)
    examples: list
    aliases: tuple = ()
    takes_input: bool = False
    needs_source: bool = True


def command(name, *, summary, usage="", params=(), examples=(), aliases=(), takes_input=False, needs_source=True):
    def register(func):
        spec = CommandSpec(name, func, summary, usage or f"/{name}", list(params), list(examples), tuple(aliases),
                           takes_input, needs_source)
        COMMANDS[name] = spec
        for alias in aliases:
            ALIASES[alias] = name
        return func
    return register


def lookup(name):
    return COMMANDS.get(ALIASES.get(name, name))


@dataclass
class Context:
    organisation: object
    user: object
    source_factory: object = None
    input_file: object = None
    session: dict = field(default_factory=dict)
    today_override: object = None

    @cached_property
    def source(self):
        if self.organisation is None:
            raise CommandError("No organisation selected. Use /orgs to list them and /use <name> to pick one.")
        return self.source_factory(self.organisation)

    @cached_property
    def settings(self):
        return self.source.settings()

    @cached_property
    def fy(self):
        return FinancialYear(self.settings["fy_end_month"], self.settings["fy_end_day"])

    @cached_property
    def today(self):
        if self.today_override:
            return self.today_override
        tz = getattr(self.organisation, "timezone", None) or "Australia/Brisbane"
        return datetime.now(ZoneInfo(tz)).date()


@dataclass
class Params:
    flags: set
    values: dict
    period: object
    positional: str


def read_params(cmd, ctx, *, flags=(), keyed=None, period=False, exclusive=()):
    """Interpret ``cmd.params`` for a command.

    ``flags``: words allowed on their own (--mtd). ``keyed``: {name: converter} for "--name value" (converter gets
    the value text and returns the value or raises CommandError). ``period``: whether a period may be given.
    ``exclusive``: groups of flags of which at most one may be used. Anything unrecognised is an error, so typos
    don't silently change the result.
    """
    keyed = keyed or {}
    found_flags, values, found_period = set(), {}, None
    for raw in cmd.params:
        text = re.sub(r"\s+", " ", raw.strip().lower())
        if text in flags:
            found_flags.add(text)
            continue
        key = next((k for k in sorted(keyed, key=len, reverse=True) if text == k or text.startswith(k + " ")), None)
        if key:
            value = raw.strip()[len(key):].strip()
            if not value:
                raise CommandError(f"--{key} needs a value, e.g. {_example(key)}.")
            values[key] = keyed[key](value, ctx)
            continue
        if period:
            try:
                found = parse_period(raw, today=ctx.today, fy=ctx.fy)
            except PeriodError as exc:
                raise CommandError(str(exc)) from None
            if found:
                if found_period:
                    raise CommandError("Give one period only (use \"--Jul 2026 to Sep 2026\" for a range).")
                found_period = found
                continue
        raise CommandError(f'/{cmd.name} doesn\'t understand "--{raw.strip()}". Type /help {cmd.name} for its options.')
    for group in exclusive:
        used = found_flags & set(group)
        if len(used) > 1:
            raise CommandError(f"Use only one of {', '.join('--' + g for g in group)}.")
    return Params(found_flags, values, found_period, cmd.text)


def _example(key):
    return {"due": "--due 14", "as at": "--as at 30/06/2026", "top": "--top 20"}.get(key, f"--{key} <value>")


def positive_int(value, ctx=None):
    if not value.isdigit() or int(value) <= 0:
        raise CommandError(f'"{value}" isn\'t a whole number of days or rows.')
    return int(value)


def as_at_date(value, ctx):
    try:
        found = parse_period(value, today=ctx.today, fy=ctx.fy)
    except PeriodError as exc:
        raise CommandError(str(exc)) from None
    if found is None:
        day = parse_date(value)
        if day is None:
            raise CommandError(f'"{value}" isn\'t a date I recognise. Try 30/06/2026.')
        return day
    return found.end
