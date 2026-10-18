"""Keep the CommandReference table in step with the command registry."""

from .models import CommandReference
from .registry import COMMANDS

# Starting how-to notes, written into a row only while its instructions are empty, so edits made in the admin win.
HOW_TO = {
    "status": "Start of day: the business snapshot (cash, revenue, debtors, bills due including overdue, unreconciled "
              "items). Add a period and a measure to compare with budget and last year, e.g. /status --September "
              "2026 --revenue --mtd.",
    "pl": "Profit and loss by account against budget and last year. Default is this month; give a period or "
          "--ytd. Add >> pl.xlsx to save it for a pack. "
          "/pl --event (or --by <dimension>) gives a column per event, Unassigned, and a check against Xero; Xero "
          "tracking and tags approved in /tag count together.",
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
    "dim": "See what you can tag by before tagging: Xero's tracking categories (Xero allows 2 active and 4 in all) "
           "and iLA's own dimensions, which have no limit and never change Xero. /dim event lists a category's "
           "options and a dimension's values with their extra fields (e.g. venue, event date).",
    "tag": "Tag transactions to a dimension value, e.g. bills and invoices to events. Choose the types and period "
           "(both dates included), narrow with filters (--desc, --contact, --account, --amount, --xero, "
           "--attachment), then either --to one value or let iLA propose one per transaction from Xero tracking, "
           "codes and names in the text, contacts and event dates. Check the preview and approve all, by "
           "confidence or by row. Xero isn't changed.",
    "splittag": "Split amounts on GL accounts across events (or any dimension) without changing Xero. Download the "
                "template (/splittag --template), write named rules (accounts, types, values with an Amount or "
                "Percent; amounts first, percentages of what's left, the rest untagged), choose it with "
                "/splittag --Sep 2026 << rules.csv, check and approve. Re-run a saved rule each month with "
                "/splittag --labour --Oct 2026.",
    "createtag": "Set up something to tag by from a CSV of its values. Pick a Xero tracking category to replace "
                 "(iLA then shows your names and details instead of codes like CCH1221; Xero isn't changed), an iLA "
                 "dimension to update, or a brand-new one. Download the starting file, fill in Code, Name, "
                 "Description and any extra columns (Venue, Event date…), choose it with <<, check the preview and "
                 "type approve.",
    "orgs": "List the organisations you can use; * marks the current one.",
    "use": "Switch organisation, e.g. /use TECC. Commands then read that organisation.",
    "connect": "Connect or reconnect Xero. Also grants any new permissions iLA needs (it will say when).",
    "clean": "Clear the results on screen. Your command history (up arrow) is kept.",
    "cancel": "Drop your open /tag preview, /createTag or /_createInvoice draft. Nothing is saved or written anywhere.",
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
