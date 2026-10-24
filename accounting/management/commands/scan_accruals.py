from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core.models import Organisation

from accounting.models import Accrual
from accounting.scan import copy_history, default_window, scan
from accounting.util import add_months, month_start


class Command(BaseCommand):
    help = ("Learn an organisation's accruals from its accrual account history (e.g. TECC, 2000) and print each "
            "profile's memo and rule. Creates the accrual set-up if there isn't one.")

    def add_arguments(self, parser):
        parser.add_argument("organisation", help="Organisation name, e.g. TECC")
        parser.add_argument("--account", help="Accrual account code(s), comma-separated (default: the set-up's, "
                                              "or 2000)")
        parser.add_argument("--months", type=int, help="Months of history (default: the set-up's, or 24)")
        parser.add_argument("--end", help="Last day of the window, YYYY-MM-DD (default: end of last month)")
        parser.add_argument("--copy", action="store_true", help="Copy the window from Xero first")

    def handle(self, *args, **opts):
        org = Organisation.objects.filter(name__iexact=opts["organisation"]).first()
        if org is None:
            raise CommandError(f"No organisation called {opts['organisation']}.")
        accrual, _ = Accrual.objects.get_or_create(organisation=org)
        if opts["account"]:
            accrual.account_codes = opts["account"]
        if opts["months"]:
            accrual.history_months = opts["months"]
        accrual.save()
        start, end = default_window(accrual.history_months)
        if opts["end"]:
            end = date.fromisoformat(opts["end"])
            start = add_months(month_start(end), 1 - accrual.history_months)
        if opts["copy"]:
            from xero.source import source_for

            copied = copy_history(accrual, source_for(org), start, end)
            self.stdout.write(f"Copied {copied.total} transactions from Xero ({copied.added} new).")
        try:
            result = scan(accrual, start, end)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{org}: {len(result.profiles)} profiles, {start:%b %Y} to {end:%b %Y}\n")
        for p in sorted(result.profiles, key=lambda p: (p.behaviour, -p.average)):
            self.stdout.write(f"[{p.get_behaviour_display()}] {p.name}  confidence {p.confidence:.0%}")
            self.stdout.write(f"  Memo: {p.memo}")
            self.stdout.write(f"  Rule: {p.rule_text}\n")

