from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core.models import Organisation

from accounting.ledger import LedgerError, check_bills, run_accruals
from accounting.models import Accrual
from accounting.util import month_end


class Command(BaseCommand):
    help = ("Run an organisation's accruals for a month and save the journal in the Expectation Ledger, or (with "
            "--check) look for bills that evidence or close an existing one.")

    def add_arguments(self, parser):
        parser.add_argument("organisation", help="Organisation name, e.g. TECC")
        parser.add_argument("--period", required=True, help="Month to close, YYYY-MM")
        parser.add_argument("--check", action="store_true", help="Only check bills for the existing journal")

    def handle(self, *args, **opts):
        accrual = Accrual.objects.filter(organisation__name__iexact=opts["organisation"]).first()
        if accrual is None:
            if not Organisation.objects.filter(name__iexact=opts["organisation"]).exists():
                raise CommandError(f"No organisation called {opts['organisation']}.")
            raise CommandError("No accrual set-up yet: run scan_accruals first.")
        year, month = (int(x) for x in opts["period"].split("-"))
        period_end = month_end(date(year, month, 1))
        try:
            if opts["check"]:
                journal = accrual.journals.get(period_end=period_end)
                self.stdout.write(f"{check_bills(journal)} lines moved on.")
            else:
                journal = run_accruals(accrual, period_end)
        except (LedgerError, accrual.journals.model.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{journal}: {journal.get_state_display()}, {journal.lines.count()} lines, "
                          f"{journal.total_expected:,.2f}")
        for line in journal.lines.all():
            self.stdout.write(f"  [{line.get_state_display()}] {line.description}: {line.expected_amount:,.2f} "
                              f"({line.basis})")
        for s in journal.skipped:
            self.stdout.write(f"  left out: {s['profile']}: {s['why']}")
