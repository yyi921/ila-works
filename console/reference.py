"""Keep the CommandReference table in step with the command registry."""

from .models import CommandReference
from .registry import COMMANDS

# Starting how-to notes, written into a row only while its instructions are empty, so edits made in the admin win.
HOW_TO = {
    "status": "Start of day: the business snapshot (cash, revenue, debtors, bills due including overdue, unreconciled "
              "items). Add a period and a measure to compare with budget and last year, e.g. /status --September "
              "2026 --revenue --mtd.",
    "pl": "Profit and loss by account against budget and last year. Default is this month; give a period or "
          "--ytd. Add >> pl.xlsx to save it for a pack.",
    "bs": "Balance sheet as at today, or --as at 30/06/2026 for a month or year end.",
    "tb": "Trial balance for tie-outs and audit files, as at today or --as at a date. Save with >> tb.xlsx.",
    "cash": "Bank balances now or as at a date. Quick check before a payment run.",
    "debtors": "Who owes what, aged by days past due. --detail lists each invoice, --top 10 the biggest. Use before "
               "chasing calls.",
    "creditors": "What we owe suppliers, aged by days past due. --detail lists each bill.",
    "bills": "Bills to pay in the next 7 days (--due 14 for two weeks), overdue first. --overdue shows only "
             "overdue ones. Use to plan the payment run.",
    "compare": "Spot unusual spend or missing revenue month by month. With no name it scans every supplier "
               "(--bill) or customer (--invoice) for High, Low, New and Missing; with a name, number or code it "
               "shows that one over the last 12 months (up to 24 with a period range). --by account groups by "
               "account; --detail lists the documents. Use /inv <number> to open any document it lists.",
    "recon": "Unreconciled Xero transactions per bank account and the oldest date. Xero's dashboard count of "
             "statement lines can be higher; see the note in the result.",
    "inv": "Open one invoice, bill or credit note by its number, with lines, payments and amount due.",
    "find": "Search contacts and invoice or bill numbers and references, e.g. /find smith.",
    "orgs": "List the organisations you can use; * marks the current one.",
    "use": "Switch organisation, e.g. /use TECC. Commands then read that organisation.",
    "connect": "Connect or reconnect Xero. Also grants any new permissions iLA needs (it will say when).",
    "clean": "Clear the results on screen. Your command history (up arrow) is kept.",
    "cancel": "Drop your open /_createInvoice draft. Nothing is written anywhere.",
    "help": "List every command (including your organisation's built ones), or /help <command> for its options.",
}


def sync_command_reference():
    """Create or refresh one row per registered command; mark rows for removed commands. Returns (created,
    updated, retired)."""
    created = updated = 0
    for order, spec in enumerate(COMMANDS.values()):
        fields = {
            "aliases": ", ".join(f"/{a}" for a in spec.aliases), "summary": spec.summary, "usage": spec.usage,
            "parameters": [list(p) for p in spec.params], "examples": list(spec.examples),
            "takes_input_file": spec.takes_input, "needs_connection": spec.needs_source, "sort_order": order,
            "in_code": True,
        }
        row, was_created = CommandReference.objects.update_or_create(name=spec.name, defaults=fields)
        if not row.instructions and spec.name in HOW_TO:
            row.instructions = HOW_TO[spec.name]
            row.save(update_fields=["instructions"])
        created += was_created
        updated += not was_created
    retired = CommandReference.objects.exclude(name__in=COMMANDS).filter(in_code=True).update(in_code=False)
    return created, updated, retired
