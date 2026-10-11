"""Command syntax:

    /command [text] [--param]... [<< input file] [>> output file]

Built commands start with "_" (/_weekly) and may be typed without the slash (_weekly).

Parameters come after ``--`` in any order and may contain spaces ("--September 2026"). ``--`` only starts a
parameter after whitespace, so values like "INV--001" survive. ``<<`` names an input file, ``>>`` an output file.
"""

import re
from dataclasses import dataclass, field

_MARKERS = re.compile(r"(\s--|<<|>>)")


class CommandSyntaxError(ValueError):
    pass


@dataclass
class ParsedCommand:
    name: str
    text: str = ""
    params: list = field(default_factory=list)
    input_file: str = ""
    output_file: str = ""
    raw: str = ""


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(line):
    raw = line.strip()
    if raw.startswith("_"):
        raw = "/" + raw  # built commands may be typed without the slash
    if not raw.startswith("/"):
        raise CommandSyntaxError('Commands start with "/". Type /help to see them.')
    m = re.match(r"/(_?[A-Za-z][\w-]*)", raw)
    if not m:
        raise CommandSyntaxError("Type a command name after the slash, e.g. /status.")
    cmd = ParsedCommand(name=m.group(1).lower(), raw=raw)

    parts = _MARKERS.split(raw[m.end():])
    head = parts[0]
    if head and not head[0].isspace():
        raise CommandSyntaxError(f"Unknown command /{cmd.name}{head.split()[0]}. Type /help to see the commands.")
    cmd.text = head.strip()
    for marker, value in zip(parts[1::2], parts[2::2]):
        marker, value = marker.strip(), value.strip()
        if marker == "--":
            if not value:
                raise CommandSyntaxError('Empty parameter: put a word after "--".')
            cmd.params.append(value)
        elif marker == "<<":
            if cmd.input_file:
                raise CommandSyntaxError("Only one input file (<<) per command.")
            if not value:
                raise CommandSyntaxError('Name the input file after "<<".')
            cmd.input_file = _unquote(value)
        else:
            if cmd.output_file:
                raise CommandSyntaxError("Only one output file (>>) per command.")
            if not value:
                raise CommandSyntaxError('Name the output file after ">>", e.g. >> report.xlsx.')
            cmd.output_file = _unquote(value)
    return cmd
