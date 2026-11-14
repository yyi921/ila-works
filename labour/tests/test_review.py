"""A month's labour: the stub estimate for days no pay run covers yet, and its replacement by the pay run."""

from datetime import date, datetime
from decimal import Decimal as D

from core.models import Adjustment
from labour import ledger, review

from .test_allocation import labour  # noqa: F401  (fixture)
from .test_labour import org, row, workbook  # noqa: F401  (fixture)


def test_stub_days_estimated_from_the_last_pay_runs_then_replaced(labour):
    org, _ = labour
    r = review.month_review(org[0], date(2026, 10, 1), date(2026, 10, 31))
    assert r.stub_days[0] == date(2026, 10, 19) and r.stub_days[-1] == date(2026, 10, 31)
    # 5-18 Oct: 8 days in an event's window (Gala 8-12, Expo 11-15) cost 789.60, 6 days without cost 63.00.
    # 19-31 Oct has no event: 13 days × 10.50.
    estimate = r.estimate
    assert (estimate.status, estimate.date, estimate.origin) == (Adjustment.Status.EXPECTED, date(2026, 10, 31),
                                                                 review.ESTIMATE)
    assert estimate.debit_total == D("136.50") and r.estimate_total == D("136.50")
    assert "Per day with an event 98.70, without 10.50" in estimate.note
    assert sum(line.amount for line in estimate.lines.all()) == 0
    assert r.pay_runs == D("852.60") and r.month_total == D("989.10")
    assert r.other == r.difference - r.not_in_xero - r.estimate_total

    again = review.month_review(org[0], date(2026, 10, 1), date(2026, 10, 31)).estimate
    assert again.pk == estimate.pk  # rebuilt in place

    rows = [row(111, "Ann Lee", "700-3000", "TECC - F&B Service", "Normal Pay", "Hours By Rate",
                datetime(2026, 10, 20), datetime(2026, 10, 20), 8, 200.00, period_end=datetime(2026, 11, 1))]
    ledger.upload(org[0], org[1], "b.xlsx", workbook(rows=rows, summary_total=D("200.00")))
    r = review.month_review(org[0], date(2026, 10, 1), date(2026, 10, 31))
    assert r.stub_days == [] and r.estimate is None
    estimate.refresh_from_db()
    assert estimate.status == Adjustment.Status.SUPERSEDED
