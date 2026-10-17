# Cross-System Command Console — MVP Plan

Oct 4, 2026 · @Erin Yin

## Product summary and selling points

The product is a keyboard-first console that lets finance teams query, report on and prepare postings across the business systems they already run, starting with Xero, without navigating dozens of web pages. It never writes directly into the ledger: it builds validated import files that a person stamps before posting, and it adds information (such as event attribution) in its own layer without changing source data.

**Key selling points**

1. **One command language across systems.** `/find`, `/bal`, `/debtors`, `/pl --by event` work the same whether the data sits in Xero, Sage Intacct or Opera. No single vendor's AI assistant can offer this, because each one only sees its own system.
2. **Speed.** Data is synced into a local store, so searches and balances return in under a second instead of after several page loads. `find smith` searches every connected system in one go.
3. **Human-stamped import files, not direct writes.** Commands build intelligent import files in the customer's preferred format and location. A person reviews and stamps each file before it is posted manually or automatically. This works even for systems with no write API, and it is a control auditors already understand.
4. **An enrichment layer that never touches the source.** Tags such as events are inferred from calendars, timesheets and bank timing, stored alongside the source records and reported against them. Every report ties back to the source system's totals to the cent.
5. **AI that translates, never acts.** The LLM turns plain English into a structured command. The person approves the command, and a deterministic engine runs it. The approved command log is the audit trail.
6. **Built by finance, for finance.** Designed by a Chartered Accountant and Financial Controller around real month-end, audit and multi-venue reporting pain, with hospitality and events as the first vertical.

**One-line pitch:** event-level P&L you can trust, and every routine finance lookup in one keystroke, without recoding Xero or opening ten tabs.

## MVP scope

Version 1 is a Xero-only web console with four jobs: search and lookups, standard reports, journal import-file preparation with stamping, and tracking-category tagging (events, departments or any other analysis dimension) with a P&L by dimension. Everything else is deferred, so the build fits eight weeks of part-time work.

**Target customer for v1:** a multi-venue hospitality or events business on Xero (roughly 2 to 20 venues or event spaces) with an in-house finance person or an external bookkeeper. Second audience: bookkeeping practices with several hospitality clients.

**In scope**

| Area | v1 commands | Notes |
| --- | --- | --- |
| Connect | `connect xero`, `disconnect`, `status` | OAuth 2.0, shows organisation name and connection state |
| Search and lookups | `/find <text>`, `/bal <account>`, `/debtors`, `/creditors`, `/inv <no>` | Served from the local store, with last-sync time shown |
| Reports | `/status`, `/pl`, `/bs`, `/tb`, `/cash`, `/bills`, `/recon`; any result `>> file.xlsx` or `.csv` | Uses Xero's Reports API, so totals match Xero exactly |
| Journal preparation | `/jnl --profile <name> << <file>`, `/stamp <id>`, `/files --pending`, `/posted <id>` | Builds the import file, opens it, records stamp and hash; manual posting only |
| Tracking-category tagging | `/dims << <file>`, `/tag --dim <name> --<period>`, `/review --tags`, `/tag set`, `/pl --by <dimension>` | Any dimension, e.g. event or department; tags live in the app; report includes Unassigned and ties to Xero P&L |
| Plain English | Input without `/` is read as plain English and drafted into a command for approval | Ships last; can slip to v1.1 without breaking anything |

**Out of scope for v1** (planned order after launch)

1. Automatic posting of stamped files via the Xero API.
2. Pushing tags back to Xero tracking categories.
3. Second connector: Sage Intacct, then Opera via OHIP.
4. Desktop app and offline mode.
5. Multi-system commands, such as reading Bepoz and posting to Xero.

## Detailed requirements

The requirements below define "done" for v1. Must-haves block launch; should-haves can ship in the weeks after.

**Functional: must-have**

1. **Connection.** Sign Up / Sign In with Xero and OAuth 2.0 with the `offline_access` scope. Show the connected organisation name and connection status, offer a disconnect button, and handle disconnection from Xero's side. These are Xero certification checkpoints, so build them correctly once.
2. **Sync.** Pull accounts, tracking categories, contacts, invoices (sales and bills, with line items), credit notes, payments, bank transactions and manual journals into Postgres. Incremental sync using modified-since filters. Respect Xero rate limits (60 calls a minute, 5 concurrent) with queued back-off.
3. **Canonical model.** Entities: `account`, `contact`, `document` (invoice, bill, credit note), `document_line`, `bank_txn`, `journal`, `tracking_option`, `dimension`, `dimension_value`, `event`, `assignment`, `tag`. Each row keeps its Xero ID for tie-back.
4. **Command engine.** Grammar `/command [text] [--param]... [<< input file] [>> output file]` (decided 10 October 2026): every command starts with `/`; parameters follow `--` in any order and may contain spaces (`--September 2026`); `<<` names an input file and `>>` saves the result (.xlsx or .csv). Unknown parameters are errors, never ignored. Autocomplete, inline help (`/help <command>`), command history and saved commands. First command set: `/status` (cash, revenue MTD/YTD, debtors, bills due in 7 days, bank reconciliation; or `/status --<period> --<measure> --mtd|qtd|ytd` against budget and last year), `/pl`, `/bs`, `/tb`, `/cash`, `/debtors`, `/creditors`, `/bills`, `/recon`, `/inv`, `/find`, `/orgs`, `/use`, `/connect`, `/help`, `/clean` (clears the screen; history kept). Figures are read live from Xero (cached five minutes) until sync lands. The console has a Standard and an Incognito (terminal-style) display, switched from the top bar. `/status` means the business snapshot only; journal files use `/files`.
5. **Search.** Full-text index across contacts, documents, lines and bank transactions. Results show type, date, amount and a link back to the record in Xero.
6. **Reports.** P&L and trial balance from Xero's Reports API, exportable to XLSX and CSV.
7. **Journal file builder.** Output profiles (template columns, date format, defaults, save location, filename pattern, open behaviour). Validation before building: balanced, account and tracking codes exist and are active, date in an open period, required fields present, "Not Given" for missing optional fields.
8. **Stamp and register.** Each file gets an ID and a lifecycle status (built, stamped, posted, rejected). Stamping re-validates, records who and when, and stores a SHA-256 hash; any later edit invalidates the stamp. `/files --pending` lists files built but not yet stamped or posted. The uploaded source file is kept with the register entry (organisation, user, hash); one-off uploads for other commands are read and discarded, never stored.
9. **Dimension tagging.** Customer-defined dimensions (event, department, venue, campaign and so on), each with its own reference data, such as an event calendar CSV for events or timesheet department codes for labour. Attribution rules by data type with configurable windows, confidence levels, a review queue, manual overrides, and a stale-tag flag when the source record changes. Design decisions (7 October 2026):
   - **Unlimited dimensions.** Xero allows two active tracking categories; iLA allows any number. A dimension may be linked to a Xero tracking category (e.g. Event), and its values to tracking options.
   - **Tag at line or transaction level; store per line.** An *assignment* records a decision for one line, or for a whole invoice, bill, credit note, bank transaction or journal, split across one or more values (shares sum to 100%). *Tags* are the per-line result that reports read, so the P&L by dimension still ties to the cent. A transaction-level assignment also covers lines added later in Xero and does not go stale when lines are edited; a line-level one does.
   - **Groups are dimension values.** Tagging a group (e.g. all invoices and bills for the Smith wedding) creates one transaction-level assignment per transaction with the same value, sharing a batch ID. No separate group object.
   - **Precedence per line:** manual, then file import, then Xero tracking, then rules; at the same level of authority, line beats transaction; then the newest wins. A manual "Unassigned" on one line overrides a transaction-level tag.
   - **iLA may override Xero tracking.** Where a dimension is linked to a tracking category, Xero's tracking comes in as confirmed line-level assignments, but a manual iLA tag wins. Every line where iLA and Xero disagree (including splits, which Xero cannot hold) is listed as a mismatch to correct in Xero via a journal or import file.
   - **When tagging runs (10 October 2026).** Manual tags apply immediately. Attribution rules run only when the user runs `/tag --dim <name> --<period>` (optionally limited to transaction types), never automatically on sync. Sync only re-applies existing decisions to the transactions it changed and refreshes Xero tracking. Reports (`/pl --by <dimension>`) never tag: they read stored tags, show Unassigned and the share resting on unconfirmed proposals, and offer the `/tag` command for the period.
   - **Custom attributes.** Each dimension defines its own fields (text, number, date, yes/no; optionally required), e.g. budget and owner for a campaign; each value stores its attributes and is validated against them.
10. **P&L by dimension.** Columns per value of the chosen dimension (each event, or each department) plus Unassigned, with a tie-out row against Xero's P&L for the same period that must show zero difference.
11. **Audit log.** Every command, approval, stamp and override is logged with user, time, input and result.

**Functional: should-have**

1. Plain-English input (requested 10 October 2026). Anything typed without a leading `/` goes to the LLM (Claude API), which reads it and translates it into iLA commands within iLA's capability: built-in commands, the organisation's shortcuts and custom measures. Example: "how did labour compare with last month?" drafts `/compare --labour`. The draft is shown for approval before it runs, keeping the rule that AI translates and never acts. The LLM may also draft a new shortcut or custom measure, which a superuser approves before it is saved. If a request is outside iLA's capability, it says so rather than guessing.
2. Supplier rules learned from repeated overrides.
3. Saved command scripts for month-end routines (a sequence of commands run together, e.g. a month-end pack).
4. Excel-safe output (XLSX with text-typed code columns) for files opened in Excel. Built 10 October 2026.
5. Superuser builder (requested 10 October 2026). A separate admin page where superusers configure iLA per organisation without code changes. Every change is audit-logged and can be reset to default.
   - **Command views.** Every built-in command keeps a default view. Superusers choose, for example, which figures `/status` shows, their order, the bills horizon and default measures and windows. Commands read these settings at run time.
   - **Custom measures.** Named groups of accounts (and optionally dimension values), e.g. *labour costs* = wages, super, payroll tax and workers' comp. A measure becomes a parameter in `/status`, `/pl` and `/compare` (`--labour`) and ties back to the P&L.
   - **Builder page and built commands (first version built 11 October 2026).** The builder is at `/console/builder/`, linked from the console's top bar. It is open to superusers and to users given "Can build custom commands"; it is scoped to the organisation in use, and every create, edit and delete is audit-logged with before and after.
     - **Naming.** Every command made from a template or from scratch starts with `_` (`/_weekly`, or just `_weekly`), so it can never clash with or replace a native iLA command.
     - **Start from scratch.** Runs up to 10 native commands in order and shows them as one report (`>>` saves every table). `{text}` and `{params}` pass on what is typed after the name, e.g. `/_supplier onemusic --Oct 2024 to Sep 2026` = `/compare --bill onemusic --Oct 2024 to Sep 2026`.
     - **Checks on save.** Steps are checked when saved: native commands only, no `/use`, `/connect` or `/clean`, no `<<` or `>>` inside steps, and no built command calling another.
     - **Where they appear.** Built commands appear in `/help`, `/help _name` and Tab completion, and can be switched off without deleting them.
     - **Templates.** Templates are listed on the builder page; `_createInvoice` (item 9) is next.
6. Exact bank reconciliation status. Xero's dashboard count of statement lines awaiting a match, and a true "reconciled to" date, need Xero's restricted Bank Statements Plus API, which requires Xero's approval. Apply as part of the certification work. Until then, `/status` and `/recon` show transactions entered in Xero but not reconciled. A fallback is `/recon << statement.csv`, which compares a bank statement export against Xero.
7. `/compare` (requested 10 October 2026; first version built the same day: bills, invoices, accounts and P&L measures; custom measures follow the superuser builder). Month-by-month comparison to spot unusual bills, invoices or costs. The default is the last 12 complete months plus the current month to date; a period parameter overrides it.
   - **What to compare:** `--bill` or `--invoice`, narrowed by number (`--bill B-0042`), contact name (`--supplier Acme`, `--customer Smith`) or contact or account code. Given a bill or invoice number, it compares that document with the same contact's documents in earlier months.
   - **Measures:** `--revenue`, `--gp`, `--expenses` and any custom measure from the superuser builder (e.g. `--labour`); several may be combined.
   - **Output:** one row per month with the amount, change against the previous month (amount and %) and against the 12-month average, with large movements flagged. `--detail` breaks each month down by account or line. Results export with `>>` like any command.
8. Choosing where `>>` saves (requested 10 October 2026). Today the browser saves to its download folder and any folder in the name is dropped. Three routes, in order: the `ila` terminal app writes to exact local paths (`>> C:\Month end\Oct\pl.xlsx`); output profiles save to cloud storage (OneDrive, SharePoint or Google Drive) for team folders, which also works on ila.works; Chrome and Edge can remember a chosen folder, with Downloads as the fallback in other browsers.
9. Invoice templates for events billing (replaces the `/recharge --event` design of 10 October; agreed 11 October 2026). Generic for any events business, so they fit TECC's process and companies with different processes and systems.
   - **`/_createInvoice` (first template, renamed from `_billcheck` on 11 October).** Builds the customer invoice as an import file from the information fed to it, and lists questions to settle before approval.
   - **Check mode (a later template).** Checks an existing draft or awaiting-approval invoice against the same information.
   - **The core: Agreed, Incurred, Billed.** It is a three-way match, like purchase order, receipt and bill in accounts payable, applied to sales:
     - **Agreed** is what the customer signed up to. At TECC that is the hire agreement and event order; elsewhere it may be a quote, a customer PO or a booking in a venue system.
     - **Incurred** is what actually happened: supplier bills, timesheets and ticketing settlements.
     - **Billed** is the draft invoice, with deposits and settlements applied.
   - **The three checks:**
     - (a) Billed against Agreed: rates, quantities, tax basis (GST inclusive or exclusive) and terms.
     - (b) Agreed or Incurred but not Billed: revenue left on the table.
     - (c) Every billed line carries the required details for later analysis: event tag, revenue account per item type, tax code and reference.
   - **Settings per organisation, made in the builder (not code):**
     - The event identifier: invoice reference, ticketing event code or job number.
     - Agreed sources and Incurred sources.
     - An item catalogue with aliases. For example, "Fire Warden" and "Fire Panel Monitor" are one item. Each item has a unit (flat, hour, day, % of receipts, per ticket), a pricing rule (fixed, rate × quantity, at cost, markup %, included in package), and a revenue account and tax code.
     - Which document decides quantity. At TECC the event order beats the agreement's estimate, and supplier bills and timesheets beat the event order.
     - The price basis, tolerances, and when the check runs. At TECC that is on the draft, before finance approves it.
     - The output format.
   - **Inputs.** The end goal is reading the invoice's attachments straight from Xero. This needs the read-only `accounting.attachments.read` permission. Draft and awaiting-approval invoices can already be read from Xero's Invoices API. The current `/compare` and `/inv` filter leaves them out, so these templates read them separately. Documents can also be pointed to with `<<`, from a local folder or a shared cloud drive. For this round, use synthetic documents only, saved locally or in Postgres.
   - **Output: draft invoice as an import file.** Xero's sales invoice CSV import creates draft invoices, with account code, tax rate and tracking, so the event tag goes in at import. MYOB and QuickBooks also import invoices; confirm their formats when needed. Import files keep iLA read-only. Creating drafts directly through the API needs write permission and is a later decision.
   - **Worked example (TECC, INV-2070; real documents kept out of git).** A manual check of the support files found the following:
     - The agreement prices everything GST inclusive, but the invoice added GST on top: $3,303.23.
     - The AV supplier's GST-inclusive cost was re-billed plus GST.
     - Ushers were billed at the agreement's 24-hour estimate against 16 hours in the event order.
     - First aid and security hours differ between documents.
     - 64 promoter complimentary tickets were not charged at $0.50 each.
     - There is no merchandise fee and no electrician question answered.
     - There is a $1,000 gap between the support working and Xero payments.
     - Contractor bills for security, first aid and cleaning were missing.
     - The event is named four different ways across the documents.

     The synthetic test set should reproduce each of these.
   - **Where the set-up lives (agreed 11 October 2026; built).** An organisation can use several systems, and each system has its own import format and its own way of making invoices. These are linked to the organisation on its admin page (Organisation → Invoice set-ups):
     - **Import format.** One per system, shared by every customer of that system. "Xero sales invoice CSV" is seeded with the 27 columns of the template downloaded from TECC and C&M, which are identical. A customised ERP gets a format of its own.
     - **Invoice set-up.** One per organisation, system and kind of invoice, e.g. TECC "Event invoices", C&M "Labour hire". It holds:
       - the import format;
       - the tax basis (TECC's own invoices mix inclusive and exclusive);
       - the date format, due days and branding theme;
       - the default account and tax rate;
       - the tracking category (Event or Job) and how the reference is written;
       - the sources of billing information;
       - the questions to ask before approval;
       - free-text instructions.
     - **Items.** Each set-up has items with name, aliases, unit (flat, hour, day, per head, each, %, at cost), rate, markup, account, tax rate and item code.
     - **Import files.** Approved files are saved under Import files with their content and can be downloaded from the admin or the website. Downloads are members-only and audit-logged.
   - **The two pilot cases.**
     - **TECC.** Invoices are made from agreements, event orders, supplier bills and ticketing settlements.
     - **C&M Steel.** The manager keeps timesheets and draft invoices on paper, someone else enters them in Xero, and who to email is agreed by phone. The invoice has one line per day ("Day Labour on 01 September", hours × $80), and the reference is the customer's job code.
   - **The `/_createInvoice` dialogue (proposed 11 October 2026, to refine together).** Typing `/_createInvoice` starts a guided conversation in the console:
     1. **Set-up.** Which invoice set-up, if the organisation has more than one.
     2. **Customer.** Searches Xero contacts and shows the closest matches to pick from, or offers a new contact.
     3. **Event or job.** Searches the tracking options and recent invoice references for the closest match.
     4. **Billing information.** Asks for each source in the set-up (e.g. "Where is the hire agreement?", "Where are the timesheets?"). The user points to a local file with `<<`; Xero attachments and shared drives come later.
     5. **Draft on screen.** iLA reads the files and maps what it finds to the set-up's items. The language model reads PDFs; spreadsheets and CSVs are read directly. Prices and totals are always calculated by iLA, not the model. The draft shows each line with its source, plus the set-up's questions and any gaps found, e.g. a $0 line or the tax basis.
     6. **Tweaks.** The user changes the draft in plain English or line by line ("security 24 hours", "remove catering") for as many rounds as needed. Each round shows what changed.
     7. **Approval.** iLA first checks the draft against the organisation's Xero data: contact, account codes, tax rates and tracking options. The import file is then built, saved under Import files with its hash, and offered for download, and the approval is audit-logged.

     While the dialogue is open, typing without `/` answers its current question; `/cancel` ends it. Drafts are saved so they can be resumed.
   - **First version built (11 October 2026, branch `command-builder`).** The dialogue above works end to end:
     - **Customer:** Xero contact search, or a new customer.
     - **Event:** suggestions from the customer's recent invoice references, matched against the set-up's tracking category.
     - **Billing information:** files with `<<` (PDF, Excel, CSV, text, images; several at once), or details typed in, e.g. C&M's hours.
     - **Reading:** the language model reads each file into billing facts (Claude API, `ANTHROPIC_API_KEY`, model `claude-sonnet-5-5`), then proposes lines and questions.
     - **Coding and amounts:** iLA codes each line from the item list, then from how the same description was coded on recent invoices, then from the set-up default. It calculates amounts and GST on the set-up's tax basis and flags $0 lines, lines not in the item list and rates that differ from the item list.
     - **Payments:** deposits and settlements are shown as payments to apply, not as invoice lines.
     - **Tweaks:** in plain English, with a list of changes each round.
     - **Approval:** blocks on a used invoice number, an inactive account or tax rate, or an unknown tracking option. The import file is built, stamped (SHA-256) and saved under Import files with a download link, and the approval is audit-logged.
   - **Decisions (11 October 2026).**
     - Source documents should in time be stored with the import file as evidence. For the November version the current rule stays: uploads are read and discarded, and only the extracted facts are kept in the draft.
     - The user approved testing with TECC's real INV-2070 documents locally, comparing the iLA import against the original invoice.
   - **Test import (11 October 2026).** `python manage.py sample_invoice_file --organisation "Demo Company (AU)" --email <address>` builds two synthetic invoices. The import into Demo Company will confirm three things:
     - Discount is a percentage: ILA-TEST-001's venue line shows 4,950.00 if it is a percentage, 9,850.00 if it is an amount.
     - Which tax basis the import screen offers.
     - That invoices land as drafts. It also checks what EmailAddress does: does it update the contact, and does Xero email anything automatically?

     1. Builder page first, for approval. Built 11 October: start-from-scratch commands, with `_createInvoice` shown as planned.
     2. Confirm the Xero import format, then build `_createInvoice` (first version built 11 October; test on TECC INV-2070 next).

**Non-functional**

| Requirement | Target for v1 |
| --- | --- |
| Lookup and search speed | Under 1 second from the local store |
| Sync freshness | Every 15 minutes, plus an on-demand `sync` command |
| Hosting | Australian region (AWS Sydney or Azure Australia East) |
| Availability | Business hours with best effort; daily backups, tested restore |
| Tenancy | Every row scoped to a customer organisation; no cross-tenant queries |
| Accessibility | Fully keyboard-operable, readable in light and dark modes |

## Key challenges

The hardest parts are not the code: they are Xero's commercial gates, clean IP ownership, and getting real customers connected within weeks.

**1. Xero tiers, certification and listing**

| Tier | Max connections | Monthly fee (AUD, ex GST) | App Store listing |
| --- | --- | --- | --- |
| Starter | 5 | Free | Not available |
| Core | 50 | $35 | Not available |
| Plus | 1,000 | $245 | Optional |
| Advanced | 10,000 | $1,445 | Optional |

Source: [Xero developer pricing FAQ](https://developer.xero.com/faq/pricing-and-policy-updates).

- New apps start on Starter. A sixth connection only needs a credit card to move to Core. Moving beyond Core requires meeting that tier's requirements, including certification.
- Certification prerequisites include Sign Up with Xero and **ten active customer connections**, followed by a review against Xero's checkpoints (connection, branding, App Launcher, scopes, error handling, data integrity, account mapping, taxes). [Building and growing your app](https://developer.xero.com/documentation/xero-app-store/app-partner-guides/building-and-growing-your-app) · [Certification checkpoints](https://developer.xero.com/documentation/xero-app-store/app-partner-guides/certification-checkpoints)
- The **Journals endpoint** (full GL journal history) is a premium feature for Advanced tier and above, with a security assessment. Manual Journals, invoices, bank transactions and reports are available on every tier. v1 is designed around those, so it does not need Journals.
- Rate limits: 60 calls a minute, 5 concurrent, and 1,000 calls a day per organisation on Starter (5,000 on higher tiers). The initial sync of a large organisation must be paced over hours.

**2. Xero's terms on AI and automation**

Xero prohibits using API data to train or contribute to AI or ML models, and prohibits bots or browser extensions that simulate user actions. v1 complies by design: the LLM only translates commands, and nothing is scraped or automated through the Xero web interface. Confirm the chosen LLM provider's commercial terms exclude customer data from training.

**3. Security**

Xero states all app partners will undergo a security assessment reviewed by its security team, covering areas such as authentication, encryption, personnel security and audit logging, with an advanced assessment at 1,000 connections. [Xero ecosystem security FAQ](https://developer.xero.com/faq/xero-ecosystem-security-requirements-update) · [Security standard](https://developer.xero.com/partner/security-standard-for-xero-api-consumers). Build to that standard from day one:

- OAuth tokens encrypted at rest, never logged or shown in the interface.
- MFA on console logins; Secure and HttpOnly session cookies; HTTPS only.
- Granular, least-privilege Xero scopes; read-only for everything in v1.
- Tenant isolation, audit logging, encrypted backups, Australian hosting.
- Customer offboarding: disconnect plus deletion of synced data within a stated period.
- A written privacy policy and data processing terms that customers can review.

**4. IP and employment** (not legal advice; confirm with a lawyer)

- Check the IP, confidentiality and outside-work clauses in the Morris Group employment contract before writing product code.
- Build in a new repository owned by AFIS Consultancy Pty Ltd, on personal equipment, outside work hours, using only synthetic and Xero demo-company data.
- Do not reuse code, mappings or data written for Morris Group work (including the cage reconciliation logic); re-implement general patterns from scratch.
- Consider disclosing the venture to Morris Group early. If Morris becomes a customer, use a written agreement with an arm's-length decision maker.
- Search the product name for trademark conflicts, and register the business name and domain under AFIS.
- Put customer terms, limitation of liability and professional indemnity insurance in place before paid use.

**5. Data requirements per customer**

| Feature | Data needed | Where it comes from |
| --- | --- | --- |
| Lookups and reports | Xero organisation data | Xero API (automatic) |
| Journal preparation | Source file in a known layout, chart of accounts, tracking categories | Customer upload plus Xero |
| Tagging: costs | Bills and lines, supplier names, dates | Xero API |
| Tagging: labour (event or department) | Shift date, hours, venue or department | Xero Payroll, or a timesheet CSV from UKG, Deputy and similar |
| Tagging: revenue | Bank receipt dates, payer, amount | Xero bank transactions |
| Event calendar | Event ID, name, venue, start and end, bump-in and bump-out dates | Customer CSV export |
| Other dimensions | Department or cost-centre codes, venue list, any customer mapping rules | Xero tracking categories, timesheet exports or customer CSV |

The event calendar is the dependency most likely to be missing or messy, so the onboarding checklist must request it first.

**6. Getting customers connected**

The launch needs at least three pilot organisations actively using the product, and certification needs ten. Pilot outreach has to start in week 1, in parallel with the build, not after it.

## Reality check on the end-of-November target

A commercially usable, paid product connected to live Xero organisations by 30 November 2026 is achievable. A listing on the Xero App Store by that date is not, because listing needs the Plus tier, which needs certification, which needs ten active customer connections and Xero's review time.

**What "available as a commercial connector" can mean on 30 November**

- The app runs in production on Xero's Core tier (up to 50 connections, A$35 a month).
- At least three pilot organisations are connected and using it, and new customers can sign up, connect Xero and pay through the product's own billing.
- Sign Up with Xero and all certification checkpoints are built, so the certification application can be lodged as soon as ten organisations are connected.

**What follows after 30 November**

Realistic App Store listing date: **March 2027**, with February only if ten customers connect before Christmas. December and January are peak trading for hospitality and events, so customer onboarding will slow. Xero's review time is not published, so the estimate below allows 3 to 6 weeks for it; confirm with Xero.

| Target | Milestone to tick off |
| --- | --- |
| Dec 2026 | Move from 3 pilots to paying customers; keep onboarding toward ten organisations |
| Dec 2026 | Self-check against every Xero certification checkpoint: connection, branding, App Launcher, scopes, error handling, data integrity, account mapping, taxes |
| Dec 2026 | Security assessment pack ready: access control, encryption, audit logging, incident response and personnel policies |
| Jan 2027 | Ten active customer connections, the certification prerequisite |
| Jan 2027 | Certification lodged, with a test account provisioned for Xero's reviewers |
| Feb 2027 | Review feedback fixed and certification granted |
| Feb 2027 | Apply to move from Core to Plus (A$245 a month) |
| Mar 2027 | Listing content ready: description, screenshots, pricing, demo video, support and privacy links |
| Mar 2027 | App Store listing published; ask pilots for the first reviews |

**Capacity assumption**

The plan assumes about 15 to 20 focused hours a week alongside a full-time role, with Claude Code doing most of the implementation and testing. If that capacity drops, cut in this order: plain-English input, then supplier-rule learning, then the AP ageing command. Never cut stamping, the tie-out check or security basics.

## Project plan and milestones

Core features are built in four one-week sprints from 5 October, with tracking-category tagging done by 25 October. Week 5 tests everything built in October and follows up the five pilot prospects, week 6 covers security, and weeks 7 and 8 run live pilots before commercial launch on 30 November 2026. Pilot outreach runs alongside the build from week 1.

&#91;embedded content: MVP timeline · 8 workstreams, 5 milestones\]

Features are built by 1 November, tested in week 5 and hardened in week 6; pilots then run live for two weeks before the 30 November launch.

**Weekly plan**

1. **Week 1 (5–11 Oct): foundations and Xero connector.** Resolve the employment IP question. Register the Xero developer account under AFIS and create the app on Starter. New repository, Django scaffold, Postgres, Australian hosting, CI with tests. Synthetic hospitality dataset loaded into the Xero demo company. Xero OAuth with offline\_access, connect and disconnect screens, organisation name and status; initial and incremental sync of all v1 objects, rate-limit queue, sync log. First five pilot invitations sent.
2. **Week 2 (12–18 Oct): console, search, reports and the first built commands.** Agreed 11 October 2026:
   - **Reprioritised 13 October 2026: tagging is this week's focus.** Xero allows only 2 active tracking categories (4 in all, archived included), which makes iLA's unlimited dimensions the selling point.
     1. `/dim` (`/dimensions`): native command listing Xero tracking categories (with slots used) and iLA dimensions, and one category's options or one dimension's values. Built 13 October.
     2. `/createTag`: native command (17 October, replaces the planned `_createTag` template) that sets up a dimension from a CSV of its values. The user picks one of three: replace an existing Xero tracking category, update an iLA dimension, or create a brand-new dimension. Replacing never changes Xero: each Xero option code (e.g. TECC's `240525ISA`, `CCH1221`) stays as the value's Code and is linked to the option, while iLA shows the Name, Description and extra fields from the file (Venue, Event date…). The CSV needs Name and normally Code (an empty Code is made from the start date and name, e.g. `261025FIR`, numbered if taken); codes are unique within a dimension ignoring case (enforced in the database, the file check and the admin page); Description and Active are optional; any other column becomes an extra field with its type inferred. iLA offers a starting file pre-filled with the Xero codes, shows a preview with problems and warnings, and saves only on `approve`. The admin Dimension page gained "Replaces a category in the accounting system" and "Name in the accounting system" (e.g. Events Department), and values gained a Description. Next: tagging transactions to these values (`/tag`, attribution rules) and reports by dimension.
     3. `/tag` (designed 17 October 2026): `/tag --dim <dimension> --invoices --bills --credits --bank --journals|--all --<period> [filters] [--to <value>]`. Periods include both end dates. Filters combine (all must match): `--desc` (description, reference or number contains; `or` between alternatives), `--contact`, `--account`, `--amount` (`>1000`, `500 to 5000`), `--xero <category> [option]` (Xero tracking already set), `--attachment` (has a file attached), `--retag` (include transactions already tagged; default skips them). Agreed with the user: journals means manual journals only (no double counting with invoices, bills and bank); whole-transaction tagging by default (line level and splits later via `--lines`); P&L lines only by default (`--account` overrides); purchase orders and reading attachment contents (AI) come later, after the core. `--to <value>` tags every match to that value (manual, confirmed). Without it iLA proposes a value per transaction from evidence, strongest first: Xero tracking, the value's code or name (or a distinctive, non-generic word of the name, e.g. "Mecca") in the text, with the nearest date breaking ties between same-named values; then, weakly (low confidence), the same contact as other transactions tagged to a value whose dates fit. The date alone never proposes a value (switched off 17 October at the user's request: overhead bills fall inside event dates); each proposal shows confidence and reason. Already-tagged transactions are listed with "Tagged in Xero" and "Tagged in iLA" columns but get no proposal unless `--retag`. Preview first; `approve`, `approve high`, `approve 1-5,8`, `skip 3`, `set 3 <code>` or `cancel`; nothing is saved before approval. `--dim` is always an iLA dimension and never changes Xero; Xero tracking is only read (`--xero`). Writing back to Xero stays a separate step via the mismatch list. Prerequisite built with it: `/tag` copies the chosen transaction types for the period from Xero into iLA (accounts, contacts, tracking, invoices, bills, credit notes, bank transactions, manual journals), which is the first piece of the Xero sync.
     4. `/pl --event` / `/pl --by <dimension>` (built 17 October 2026, delivers must-have 10): a column per value, Unassigned, Total in iLA, Xero P&L and Difference. Xero tracking and iLA tags are one set (tracking is copied in as tracking tags; approved iLA tags win). The period is copied from Xero first. Amounts are built per line (net of tax, base currency, splits to the cent). The Difference column is what iLA doesn't read yet (on TECC September 2026 only depreciation, $7,475.34; everything else tied to the cent). Decisions are visible in admin (Core > Assignments, Console > Tag runs).
     5. `/_createInvoice` from timesheets (C&M Steel's labour hire case: hours per day × rate, one line per day, customer job code as reference, send-to contacts).
     6. `_checkBill` (check a drafted or awaiting-approval invoice against agreements, orders and costs), then `_recFA` (scope to confirm).
   - Native commands are listed in the admin (Console → Native commands, refreshed from code on every migrate, with editable how-to notes).

   Original plan for this week: Command parser, autocomplete and help. `/find`, `/bal`, `/debtors`, `/inv`, `/pl`, `/tb`, `>>` export (most of these built 10 October 2026). Full-text search index. Add-on: an `ila` terminal shell (Python, prompt\_toolkit) calling the same API, with persistent history, up/down arrows, Ctrl+R search, autocomplete, and `?` for plain-English questions that draft a command for approval. The web console stays the focus; the shell is for power users.
3. **Week 3 (19–25 Oct): tracking-category tagging.** Dimension setup (event calendar, department codes), attribution rules for bills, labour and receipts, confidence levels, review queue, overrides, `/pl --by <dimension>` with the tie-out row.
4. **Week 4 (26 Oct–1 Nov): journal files and stamping.** Output profiles, validation rules, file builder, open-after-build, register, `/stamp` with hash, `/files --pending`, `/posted`.
5. **Week 5 (2–8 Nov): testing and pilot follow-up.** End-to-end testing of every feature built in October on the demo company and synthetic data, defect fixes, tie-out checks on the event and department P&L. Follow-up conversations with the five pilot prospects to confirm start dates, data (event calendars, department codes) and Xero access.
6. **Week 6 (9–15 Nov): security and certification readiness.** Token encryption, MFA, audit log, backups and restore test, Sign Up with Xero, checkpoint review. Plain-English input if time allows. First pilot connected.
7. **Weeks 7–8 (16–29 Nov): pilots live.** Onboard at least three pilots, fix issues weekly, load real event calendars, build billing, terms, landing page and demo video.
8. **30 November: commercial launch** on Xero's Core tier with sign-up and billing open.

**Gates**

- **M1, 11 Oct:** IP position confirmed, Xero app created and demo company syncing. If IP is unclear, pause code until it is resolved.
- **M2, 25 Oct:** console and tracking-category tagging working end to end on the demo company.
- **M3, 1 Nov:** feature complete for v1 must-haves; testing signed off by 8 Nov.
- **M4, 15 Nov:** first pilot organisation connected to production.
- **Launch, 30 Nov:** three pilots active, security basics in place, billing live.
- **After launch:** ten active connections, certification lodged, Plus tier and App Store listing, targeting March 2027.

## Go-to-market and pilot pitch

Lead with event-level P&L, because it has a visible, measurable payoff; the command console is how customers experience it, not the headline.

The same tagging engine covers any tracking category, so the second demo is labour cost by department: shifts tagged from timesheet department codes and reported against Xero's wages accounts.

**Pilot target: 5 to 10 conversations to land at least 3 pilots by mid-November**

- Hospitality and events businesses on Xero in North Queensland and beyond: function centres, event venues, multi-venue hospitality groups, festival and event operators.
- Bookkeepers and accountants with several hospitality clients, reached through AFIS Consultancy's network and CA ANZ contacts.
- Each pilot gets free use until 31 January 2027 in exchange for weekly feedback and permission to use an anonymised case study.

**Pitch (60 seconds)**

> You already know your total P&L from Xero. What you can't easily see is whether each event made money, because costs, wages and receipts aren't tagged to events, and recoding them in Xero takes hours. Connect Xero, upload your event calendar, and the system attributes bills, shifts and receipts to each event for you, flags the uncertain ones for a quick review, and gives you an event P&L that ties back to Xero to the cent. Nothing in your Xero file changes. And for everyday lookups, balances, ageing and reports, you type one line instead of clicking through ten screens.

**Pricing hypothesis to test with pilots** (not validated)

- Per connected Xero organisation, monthly, with a lower price per organisation for bookkeeping practices.
- Ask each pilot two questions: what does producing an event P&L cost you today in hours, and what would you pay to have it automatically each month?

**Launch assets needed by 30 November**

1. Landing page with sign-up, pricing and a 2-minute demo video recorded on the Xero demo company.
2. Onboarding checklist (connect Xero, upload event calendar, choose output profile).
3. Terms of service, privacy policy, data processing terms.
4. Short help reference listing every v1 command.

## Risks, decisions and next steps

The biggest risk to 30 November is not getting three pilots connected in time, so outreach starts this week alongside the build.

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Fewer than 3 pilots by mid-November | Launch without real usage | Start outreach in week 1; offer free use to 31 Jan 2027 |
| Employment contract restricts outside IP | Product ownership unclear | Review clauses and get advice in week 1, before writing code |
| Build time competes with month-end at work | Milestones slip | Fixed weekly hours; cut order defined; Claude Code for implementation |
| Pilot event calendars are missing or messy | Event P&L unconvincing | Request the calendar first; provide a CSV template |
| Xero's own AI (JAX, XeroForce) adds event views | Weaker Xero-only story | Keep the cross-system roadmap visible; Sage Intacct connector next |
| Large organisations hit rate limits on first sync | Slow onboarding | Paced background sync with progress shown |
| Excel edits break import files | Bad postings | Stamp-time re-validation; XLSX output with text-typed codes |

**Decisions needed this week**

- [ ] Confirm employment contract position on outside IP and get legal advice if unclear
- [ ] Product name, domain and AFIS business-name registration
- [ ] Hosting provider and region (AWS Sydney or Azure Australia East)
- [ ] Stack: Django backend holding the command engine behind an API; web console in HTMX or React first, Python terminal client (Textual) later; new Xero developer account and app under an AFIS email, separate from the bank reconciliation app
- [ ] LLM provider for plain-English input, and confirm its data-training terms
- [ ] Billing provider (for example Stripe) and pilot terms
- [ ] Shortlist of 10 pilot prospects

**First three actions**

1. Register a Xero developer account under AFIS and create the app on the Starter tier, connected to the Xero demo company.
2. Create the new repository and ask Claude Code to scaffold the Django project, models for the canonical entities, and the Xero OAuth flow.
3. Send the first five pilot invitations using the pitch above.

**Sources** (as of 4 October 2026)

- [Xero developer pricing and policy FAQ](https://developer.xero.com/faq/pricing-and-policy-updates)
- [Xero: building and growing your app](https://developer.xero.com/documentation/xero-app-store/app-partner-guides/building-and-growing-your-app)
- [Xero certification checkpoints](https://developer.xero.com/documentation/xero-app-store/app-partner-guides/certification-checkpoints)
- [Xero ecosystem security requirements FAQ](https://developer.xero.com/faq/xero-ecosystem-security-requirements-update)
- [Xero security standard for API consumers](https://developer.xero.com/partner/security-standard-for-xero-api-consumers)
