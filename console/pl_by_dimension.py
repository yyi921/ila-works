"""``/pl --by event`` (or ``/pl --event``): profit and loss with a column per value of an iLA dimension, plus
Unassigned, iLA's total, Xero's P&L and the difference.

Tags come from one place: iLA's live tags, which already combine Xero tracking (copied in as tracking tags for a
dimension that replaces a Xero category) with the tags approved in iLA (which win over Xero's tracking). Before
reporting, the period's transactions are copied from Xero, so nothing tagged in Xero since the last copy is missed.

Amounts are built line by line from invoices, bills, credit notes, bank transactions and manual journals: net of
tax, in the base currency, signed as Xero's P&L shows them (income and costs both positive). A line split across
values is divided to the cent. Xero's own P&L is the check: anything iLA doesn't read yet (e.g. payroll,
depreciation, expense claims, currency gains) shows in the Difference column, so the report always ties to Xero.
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from core.models import (
    Account,
    BankTransaction,
    BankTransactionLine,
    Document,
    DocumentLine,
    Journal,
    JournalLine,
    LineAmountType,
    Tag,
)
from core.tagging import split_amount
from xero.source import CATEGORY_OF_TYPE, INCOME_CATEGORIES
from xero.sync import KINDS, copy_period

from .output import MONEY, PCT, TEXT, Column, Figure, Figures, Note, Result, Table

ZERO = Decimal("0")
CENT = Decimal("0.01")
SECTIONS = [("revenue", "Revenue"), ("other_income", "Other income"), ("cost_of_sales", "Cost of sales"),
            ("expenses", "Expenses")]
UNASSIGNED = None


def _debit_sign(parent):
    """+1 when the parent's lines are debits (bills, spend money), -1 when credits (invoices, receive money)."""
    if isinstance(parent, Document):
        if parent.type == Document.Type.CREDIT_NOTE:
            return 1 if parent.ledger == Document.Ledger.RECEIVABLE else -1
        return -1 if parent.ledger == Document.Ledger.RECEIVABLE else 1
    if isinstance(parent, BankTransaction):
        return -1 if parent.direction == BankTransaction.Direction.RECEIVE else 1
    return 1


def _base(amount, parent, base_currency):
    rate = getattr(parent, "currency_rate", None)
    currency = getattr(parent, "currency_code", "") or base_currency
    if rate and currency != base_currency:
        amount = amount / rate
    return amount


def _lines(organisation, start, end):
    """(account, P&L amount as Xero shows it, line field, line id) for every P&L line in the period."""
    base_currency = organisation.base_currency
    sources = [
        (DocumentLine.objects.filter(document__date__gte=start, document__date__lte=end,
                                     document__status__in=(Document.Status.AUTHORISED, Document.Status.PAID))
         .select_related("document", "account"), "document", "document_line"),
        (BankTransactionLine.objects.filter(bank_transaction__date__gte=start, bank_transaction__date__lte=end,
                                            bank_transaction__status=BankTransaction.Status.AUTHORISED)
         .select_related("bank_transaction", "account"), "bank_transaction", "bank_transaction_line"),
        (JournalLine.objects.filter(journal__date__gte=start, journal__date__lte=end,
                                    journal__status=Journal.Status.POSTED)
         .select_related("journal", "account"), "journal", "journal_line"),
    ]
    for queryset, parent_field, line_field in sources:
        for line in queryset.filter(organisation=organisation, account__isnull=False):
            category = CATEGORY_OF_TYPE.get(line.account.source_type)
            if category is None:
                continue
            parent = getattr(line, parent_field)
            if isinstance(line, JournalLine):
                debit = line.amount
                if parent.line_amount_type == LineAmountType.INCLUSIVE:
                    debit -= line.tax_amount
            else:
                net = line.line_amount
                if parent.line_amount_type == LineAmountType.INCLUSIVE:
                    net -= line.tax_amount
                debit = _debit_sign(parent) * _base(net, parent, base_currency)
            amount = -debit if category in INCOME_CATEGORIES else debit
            yield line.account, category, amount.quantize(CENT, ROUND_HALF_UP), line_field, line.pk


def _tags(organisation, dimension):
    """{(line field, line id): [(value id, allocation), ...]} for the live tags in ``dimension``."""
    tags = defaultdict(list)
    rows = (Tag.objects.filter(organisation=organisation, dimension=dimension).exclude(status__in=Tag.NOT_LIVE)
            .values_list("document_line_id", "bank_transaction_line_id", "journal_line_id", "dimension_value_id",
                         "allocation").order_by("pk"))
    for doc_line, bank_line, journal_line, value_id, allocation in rows:
        key = (("document_line", doc_line) if doc_line else ("bank_transaction_line", bank_line) if bank_line
               else ("journal_line", journal_line))
        tags[key].append((value_id, allocation))
    return tags


def report(ctx, dimension, period):
    org = ctx.organisation
    copied = copy_period(org, ctx.source, KINDS, period.start, period.end)
    xero = ctx.source.profit_and_loss(period.start, period.end)
    tags = _tags(org, dimension)

    cells = defaultdict(lambda: ZERO)  # (account id, value id or None) -> amount
    categories, accounts = {}, {}
    for account, category, amount, line_field, line_id in _lines(org, period.start, period.end):
        accounts[account.pk], categories[account.pk] = account, category
        shares = tags.get((line_field, line_id))
        if not shares:
            cells[(account.pk, UNASSIGNED)] += amount
            continue
        for (value_id, _), part in zip(shares, split_amount(amount, [a for _, a in shares])):
            cells[(account.pk, value_id)] += part

    by_source_id = {a.source_id: a for a in Account.objects.filter(organisation=org, source_id__in=xero.by_account)}
    xero_amounts = {}
    for source_id, amount in xero.by_account.items():
        account = by_source_id.get(source_id)
        if account is None:
            continue
        accounts[account.pk] = account
        categories.setdefault(account.pk, xero.category_of.get(source_id) or CATEGORY_OF_TYPE.get(account.source_type))
        xero_amounts[account.pk] = amount

    used = {value_id for (_, value_id), amount in cells.items() if value_id is not UNASSIGNED and amount}
    values = sorted(dimension.values.filter(pk__in=used), key=lambda v: (_start(v, dimension), v.code.lower()))
    columns = [v.pk for v in values] + [UNASSIGNED]

    result = Result(f"Profit and loss by {dimension.name}: {period.label}",
                    subtitle="iLA tags (Xero tracking included) · checked against Xero's P&L")
    table = Table([Column("Code"), Column("Account")]
                  + [Column(f"{v.code} {v.name}" if v.name != v.code else v.code, MONEY) for v in values]
                  + [Column("Unassigned", MONEY), Column("Total in iLA", MONEY), Column("Xero P&L", MONEY),
                     Column("Difference", MONEY)])
    totals = {}
    for category, title in SECTIONS:
        ids = sorted((a for a, c in categories.items() if c == category),
                     key=lambda a: (accounts[a].code, accounts[a].name))
        if not ids:
            continue
        table.add("", title, *([None] * (len(columns) + 3)), total=True)
        section = defaultdict(lambda: ZERO)
        for account_id in ids:
            row = [cells.get((account_id, c), ZERO) for c in columns]
            ila, xer = sum(row, ZERO), xero_amounts.get(account_id, ZERO)
            if not any(row) and not xer:
                continue
            for c, amount in zip(columns, row):
                section[c] += amount
            section["ila"] += ila
            section["xero"] += xer
            account = accounts[account_id]
            table.add(account.code, account.name, *row, ila, xer, xer - ila)
        totals[category] = section
        _total_row(table, f"Total {title.lower()}", columns, section)
        if category == "cost_of_sales":
            _total_row(table, "Gross profit", columns, _combine(totals, ["revenue"], ["cost_of_sales"], columns))
    net = _combine(totals, list(INCOME_CATEGORIES), ["cost_of_sales", "expenses"], columns)
    other = xero.unattributed
    if other:
        table.add("", "Other items in Xero not linked to an account, e.g. currency gains (losses)",
                  *([ZERO] * len(columns)), ZERO, other, other)
        net["xero"] += other
    _total_row(table, "Net profit", columns, net)

    income = sum((totals.get(c, {}).get("ila", ZERO) for c in INCOME_CATEGORIES), ZERO)
    income_tagged = income - sum((totals.get(c, {}).get(UNASSIGNED, ZERO) for c in INCOME_CATEGORIES), ZERO)
    costs = sum((totals.get(c, {}).get("ila", ZERO) for c in ("cost_of_sales", "expenses")), ZERO)
    costs_tagged = costs - sum((totals.get(c, {}).get(UNASSIGNED, ZERO) for c in ("cost_of_sales", "expenses")),
                               ZERO)
    difference = net["xero"] - net["ila"]
    result.add(Figures([
        Figure(f"{dimension.name} values with amounts", str(len(values)), TEXT),
        Figure("Income tagged", income_tagged / income if income else None, PCT,
               detail="of income in iLA's copy"),
        Figure("Costs tagged", costs_tagged / costs if costs else None, PCT, detail="of costs in iLA's copy"),
        Figure("Net profit (Xero)", net["xero"], MONEY),
        Figure("Difference to Xero", difference, MONEY,
               detail="Ties to Xero" if not difference else "Transactions iLA doesn't read yet"),
    ]))
    result.add(table)
    if difference:
        gaps = sorted(((accounts[a], xero_amounts.get(a, ZERO) - sum((cells.get((a, c), ZERO) for c in columns),
                                                                      ZERO)) for a in accounts), key=lambda p: -abs(p[1]))
        gaps = [f"{acc.code} {acc.name} {gap:,.2f}".strip() for acc, gap in gaps if gap][:6]
        result.add(Note("The Difference column is what's in Xero's P&L but not in the transactions iLA reads "
                        "(invoices, bills, credit notes, bank transactions and manual journals): usually payroll, "
                        "depreciation, expense claims, inventory adjustments or currency gains. Those amounts can't "
                        "be split by " + dimension.name.lower() + " yet. Largest: " + "; ".join(gaps) + "."))
    result.add(Note(f"Read from Xero first: {copied.added} new, {copied.updated} changed and {copied.unchanged} "
                    f"unchanged transaction(s). Unassigned is what isn't tagged to any {dimension.name.lower()}; "
                    f"tag it with /tag --dim {dimension.code} --all --{period.label}."))
    return result


def _start(value, dimension):
    dates = [f["key"] for f in dimension.attribute_fields if f.get("type") == "date"]
    return next((value.attributes[k] for k in dates if value.attributes.get(k)), "9999")


def _combine(totals, plus, minus, columns):
    out = defaultdict(lambda: ZERO)
    for key in list(columns) + ["ila", "xero"]:
        out[key] = (sum((totals.get(c, {}).get(key, ZERO) for c in plus), ZERO)
                    - sum((totals.get(c, {}).get(key, ZERO) for c in minus), ZERO))
    return out


def _total_row(table, label, columns, section):
    table.add("", label, *[section.get(c, ZERO) for c in columns], section["ila"], section["xero"],
              section["xero"] - section["ila"], total=True)
