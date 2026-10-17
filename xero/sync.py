"""Copy Xero transactions for a period into iLA's own tables, so they can be tagged and reported on.

Only posted transactions are copied: invoices, bills and credit notes that are approved or paid, bank transactions
(not transfers) and posted manual journals. A transaction already copied is updated when Xero's copy changes and
marked by its new status if it was voided or deleted. Unchanged transactions are left alone (compared by a hash of
Xero's record), so running it again is cheap.

After a transaction changes, its tags are re-applied: Xero tracking is refreshed for every iLA dimension linked to a
tracking category, and transaction-level decisions cover any new lines.
"""

import hashlib
import json
from dataclasses import dataclass, field

from django.db import transaction

from core import tagging
from core.models import (
    Account,
    BankTransaction,
    BankTransactionLine,
    Contact,
    Dimension,
    Document,
    DocumentLine,
    Journal,
    JournalLine,
    LineAmountType,
    SourceSystem,
    TrackingCategory,
    TrackingOption,
)

from .api import record_date, to_decimal
from .source import CATEGORY_OF_TYPE

KINDS = ("invoices", "bills", "credits", "bank", "journals")
XERO = SourceSystem.XERO
POSTED_DOCUMENT = {"AUTHORISED", "PAID"}
DOCUMENT_STATUS = {"DRAFT": "draft", "SUBMITTED": "submitted", "AUTHORISED": "authorised", "PAID": "paid",
                   "VOIDED": "voided", "DELETED": "deleted"}
BANK_STATUS = {"AUTHORISED": "authorised", "VOIDED": "voided", "DELETED": "deleted"}
JOURNAL_STATUS = {"DRAFT": "draft", "POSTED": "posted", "VOIDED": "voided", "DELETED": "deleted",
                  "ARCHIVED": "voided"}
AMOUNT_TYPE = {"Exclusive": LineAmountType.EXCLUSIVE, "Inclusive": LineAmountType.INCLUSIVE,
               "NoTax": LineAmountType.NO_TAX}
ASSET_TYPES = {"BANK", "CURRENT", "FIXED", "INVENTORY", "NONCURRENT", "PREPAYMENT"}
LIABILITY_TYPES = {"CURRLIAB", "LIABILITY", "TERMLIAB", "PAYGLIABILITY", "SUPERANNUATIONLIABILITY",
                   "WAGESPAYABLELIABILITY"}


@dataclass
class CopyResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_lines: list = field(default_factory=list)  # lines whose account code isn't in the chart

    @property
    def total(self):
        return self.added + self.updated + self.unchanged


def copy_period(organisation, source, kinds, start, end):
    """Copy the chosen ``kinds`` (see KINDS) dated ``start`` to ``end``, both included. Returns a CopyResult."""
    copier = _Copier(organisation, source)
    result = CopyResult()
    if {"invoices", "bills"} & set(kinds):
        wanted = {t for k, t in (("invoices", "ACCREC"), ("bills", "ACCPAY")) if k in kinds}
        for record in source.raw_invoices(start, end):
            if record.get("Type") in wanted:
                copier.document(record, record["InvoiceID"], result)
    if "credits" in kinds:
        for record in source.raw_credit_notes(start, end):
            copier.document(record, record["CreditNoteID"], result)
    if "bank" in kinds:
        for record in source.raw_bank_transactions(start, end):
            if not record.get("Type", "").endswith("TRANSFER"):
                copier.bank(record, result)
    if "journals" in kinds:
        for record in source.raw_manual_journals(start, end):
            copier.journal(record, result)
    return result


def _hash(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


def account_class(xero_account):
    explicit = (xero_account.get("Class") or "").lower()
    if explicit in Account.AccountClass.values:
        return explicit
    kind = xero_account.get("Type", "")
    category = CATEGORY_OF_TYPE.get(kind)
    if category in ("revenue", "other_income"):
        return Account.AccountClass.REVENUE
    if category:
        return Account.AccountClass.EXPENSE
    if kind in LIABILITY_TYPES:
        return Account.AccountClass.LIABILITY
    if kind in ASSET_TYPES:
        return Account.AccountClass.ASSET
    return Account.AccountClass.EQUITY


class _Copier:
    def __init__(self, organisation, source):
        self.org, self.source = organisation, source
        self.accounts = self._accounts()
        self.tracking = self._tracking()
        self.contacts = {}
        self.linked_dimensions = list(Dimension.objects.filter(organisation=organisation, is_active=True,
                                                               tracking_category__isnull=False))

    # --- Reference data ---

    def _accounts(self):
        existing = {a.source_id: a for a in Account.objects.filter(organisation=self.org, source_system=XERO)}
        by_code, by_id = {}, {}
        for a in self.source.raw_accounts():
            row = existing.get(a["AccountID"]) or Account(organisation=self.org, source_system=XERO,
                                                          source_id=a["AccountID"])
            fields = {"code": a.get("Code", ""), "name": a.get("Name", ""), "account_class": account_class(a),
                      "source_type": a.get("Type", ""), "is_bank_account": a.get("Type") == "BANK",
                      "tax_code": a.get("TaxType", ""), "currency_code": a.get("CurrencyCode", ""),
                      "is_active": a.get("Status", "ACTIVE") == "ACTIVE"}
            if row.pk is None or any(getattr(row, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(row, k, v)
                row.save()
            by_id[a["AccountID"]] = row
            if row.code:
                by_code[row.code.lower()] = row
        return {"code": by_code, "id": by_id}

    def _tracking(self):
        """{(category name, option name) in lower case: TrackingOption}, mirroring every Xero category."""
        result = {}
        for c in self.source.tracking_category_list():
            category = (TrackingCategory.objects.filter(organisation=self.org, source_system=XERO, source_id=c["id"])
                        .first() if c.get("id") else None) or \
                TrackingCategory.objects.filter(organisation=self.org, name__iexact=c["name"]).first() or \
                TrackingCategory(organisation=self.org, source_system=XERO, source_id=c.get("id", ""))
            category.name, category.is_active = c["name"], c["status"] == "Active"
            category.save()
            existing = {o.name.lower(): o for o in category.options.all()}
            for o in c["options"]:
                option = existing.get(o["name"].lower()) or TrackingOption(
                    organisation=self.org, category=category, source_system=XERO, source_id=o.get("id", ""))
                active = o["status"] == "Active"
                if option.pk is None or option.name != o["name"] or option.is_active != active:
                    option.name, option.is_active = o["name"], active
                    option.save()
                result[(c["name"].lower(), o["name"].lower())] = option
        return result

    def contact(self, data):
        if not data or not data.get("ContactID"):
            return None
        cid = data["ContactID"]
        if cid not in self.contacts:
            row = Contact.objects.filter(organisation=self.org, source_system=XERO, source_id=cid).first()
            if row is None:
                row = Contact.objects.create(organisation=self.org, source_system=XERO, source_id=cid,
                                             name=data.get("Name", "")[:255] or "(no name)")
            elif data.get("Name") and row.name != data["Name"]:
                row.name = data["Name"][:255]
                row.save(update_fields=["name", "updated_at"])
            self.contacts[cid] = row
        return self.contacts[cid]

    def account(self, line):
        if line.get("AccountID") and line["AccountID"] in self.accounts["id"]:
            return self.accounts["id"][line["AccountID"]]
        return self.accounts["code"].get((line.get("AccountCode") or "").lower())

    def options(self, line):
        found = []
        for t in line.get("Tracking") or []:
            option = self.tracking.get(((t.get("Name") or "").lower(), (t.get("Option") or "").lower()))
            if option is not None:
                found.append(option)
        return found

    # --- Transactions ---

    def _upsert(self, model, source_id, posted, record, fields, result):
        """The row to (re)write, or None when it is unchanged, or not posted and never copied."""
        row = model.objects.filter(organisation=self.org, source_system=XERO, source_id=source_id).first()
        digest = _hash(record)
        if row is None and not posted:
            return None
        if row is not None and row.source_hash == digest:
            result.unchanged += 1
            return None
        if row is None:
            row = model(organisation=self.org, source_system=XERO, source_id=source_id)
            result.added += 1
        else:
            result.updated += 1
        for k, v in fields.items():
            setattr(row, k, v)
        row.source_hash = digest
        row.save()
        return row

    def _lines(self, parent, line_model, parent_field, items, result, make):
        existing = {line.source_id: line for line in getattr(parent, "lines").all()}
        keep = set()
        for number, item in enumerate(items, 1):
            line_id = item.get("LineItemID") or f"{parent.source_id}:{number}"
            fields = make(item, number)
            if fields is None:
                result.skipped_lines.append((str(parent), item.get("AccountCode", "")))
                continue
            digest = _hash(item)
            line = existing.get(line_id) or line_model(organisation=self.org, source_system=XERO,
                                                       source_id=line_id, **{parent_field: parent})
            if line.pk is None or line.source_hash != digest:
                for k, v in fields.items():
                    setattr(line, k, v)
                line.source_hash = digest
                line.save()
                line.tracking.set(self.options(item))
            keep.add(line_id)
        for line_id, line in existing.items():
            if line_id not in keep:
                line.delete()

    def _source_line(self, item, number):
        return {"line_number": number, "description": item.get("Description", ""),
                "item_code": item.get("ItemCode", ""),
                "quantity": to_decimal(item["Quantity"]) if item.get("Quantity") not in (None, "") else None,
                "unit_amount": to_decimal(item["UnitAmount"]) if item.get("UnitAmount") not in (None, "") else None,
                "account": self.account(item),
                "tax_code": item.get("TaxType", ""), "tax_amount": to_decimal(item.get("TaxAmount")),
                "line_amount": to_decimal(item.get("LineAmount"))}

    def _after(self, parent):
        for dimension in self.linked_dimensions:
            tagging.assign_from_tracking(parent, dimension)
        tagging.apply_assignments(parent)

    @transaction.atomic
    def document(self, record, source_id, result):
        kind = record.get("Type", "")
        doc_type = {"ACCREC": "invoice", "ACCPAY": "bill"}.get(kind, "credit_note")
        ledger = "receivable" if kind.startswith("ACCREC") else "payable"
        fields = {
            "type": doc_type, "ledger": ledger,
            "number": record.get("InvoiceNumber") or record.get("CreditNoteNumber") or "",
            "reference": (record.get("Reference") or "")[:255], "contact": self.contact(record.get("Contact")),
            "status": DOCUMENT_STATUS.get(record.get("Status", ""), "draft"), "date": record_date(record),
            "due_date": record_date(record, "DueDate"), "currency_code": record.get("CurrencyCode", ""),
            "currency_rate": to_decimal(record.get("CurrencyRate")) if record.get("CurrencyRate") else None,
            "line_amount_type": AMOUNT_TYPE.get(record.get("LineAmountTypes"), ""),
            "subtotal": to_decimal(record.get("SubTotal")), "total_tax": to_decimal(record.get("TotalTax")),
            "total": to_decimal(record.get("Total")), "amount_due": to_decimal(record.get("AmountDue")),
            "amount_paid": to_decimal(record.get("AmountPaid")),
            "amount_credited": to_decimal(record.get("AmountCredited")),
            "has_attachments": bool(record.get("HasAttachments")),
        }
        row = self._upsert(Document, source_id, record.get("Status") in POSTED_DOCUMENT, record, fields, result)
        if row is not None:
            self._lines(row, DocumentLine, "document", record.get("LineItems") or [], result,
                        lambda item, n: self._source_line(item, n))
            self._after(row)

    @transaction.atomic
    def bank(self, record, result):
        bank_account = self.accounts["id"].get((record.get("BankAccount") or {}).get("AccountID", ""))
        if bank_account is None:
            return
        fields = {
            "direction": "receive" if record.get("Type", "").startswith("RECEIVE") else "spend",
            "source_type": record.get("Type", ""), "bank_account": bank_account,
            "contact": self.contact(record.get("Contact")), "date": record_date(record),
            "reference": (record.get("Reference") or "")[:255],
            "status": BANK_STATUS.get(record.get("Status", ""), "authorised"),
            "is_reconciled": bool(record.get("IsReconciled")), "currency_code": record.get("CurrencyCode", ""),
            "line_amount_type": AMOUNT_TYPE.get(record.get("LineAmountTypes"), ""),
            "subtotal": to_decimal(record.get("SubTotal")), "total_tax": to_decimal(record.get("TotalTax")),
            "total": to_decimal(record.get("Total")), "has_attachments": bool(record.get("HasAttachments")),
        }
        row = self._upsert(BankTransaction, record["BankTransactionID"], record.get("Status") == "AUTHORISED",
                           record, fields, result)
        if row is not None:
            self._lines(row, BankTransactionLine, "bank_transaction", record.get("LineItems") or [], result,
                        lambda item, n: self._source_line(item, n))
            self._after(row)

    @transaction.atomic
    def journal(self, record, result):
        fields = {
            "date": record_date(record), "narration": record.get("Narration", ""),
            "status": JOURNAL_STATUS.get(record.get("Status", ""), "draft"),
            "line_amount_type": AMOUNT_TYPE.get(record.get("LineAmountTypes"), ""),
            "has_attachments": bool(record.get("HasAttachments")),
        }
        row = self._upsert(Journal, record["ManualJournalID"], record.get("Status") == "POSTED", record, fields,
                           result)
        if row is None:
            return

        def make(item, number):
            account = self.account(item)
            if account is None:
                return None
            return {"line_number": number, "account": account, "description": item.get("Description", ""),
                    "amount": to_decimal(item.get("LineAmount")), "tax_code": item.get("TaxType", ""),
                    "tax_amount": to_decimal(item.get("TaxAmount"))}

        self._lines(row, JournalLine, "journal", record.get("JournalLines") or [], result, make)
        self._after(row)
