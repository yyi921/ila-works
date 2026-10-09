# Cross-System Command Console — MVP Plan

Oct 4, 2026 · @Erin Yin

## Product summary and selling points

The product is a keyboard-first console that lets finance teams query, report on and prepare postings across the business systems they already run, starting with Xero, without navigating dozens of web pages. It never writes directly into the ledger: it builds validated import files that a person stamps before posting, and it adds information (such as event attribution) in its own layer without changing source data.

**Key selling points**

1. **One command language across systems.** `find`, `bal`, `ar ageing`, `report pl --by event` work the same whether the data sits in Xero, Sage Intacct or Opera. No single vendor's AI assistant can offer this, because each one only sees its own system.
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
| Search and lookups | `find <text>`, `bal <account>`, `ar ageing`, `ap ageing`, `show inv <no>` | Served from the local store, with last-sync time shown |
| Reports | `report pl`, `report tb`, `export <report> --to xlsx/csv` | Uses Xero's Reports API, so totals match Xero exactly |
| Journal preparation | `post jnl --from <file> --profile <name>`, `stamp <id>`, `status --pending`, `mark posted <id>` | Builds the import file, opens it, records stamp and hash; manual posting only |
| Tracking-category tagging | `dims import`, `tag --dim <name> --period`, `review tags`, `tag set`, `report pl --by <dimension>` | Any dimension, e.g. event or department; tags live in the app; report includes Unassigned and ties to Xero P&L |
| Plain English | Natural-language box that drafts a command for approval | Ships last; can slip to v1.1 without breaking anything |

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
4. **Command engine.** Grammar `verb entity [filters] [options]`, with autocomplete, inline help (`help <command>`), command history and saved commands.
5. **Search.** Full-text index across contacts, documents, lines and bank transactions. Results show type, date, amount and a link back to the record in Xero.
6. **Reports.** P&L and trial balance from Xero's Reports API, exportable to XLSX and CSV.
7. **Journal file builder.** Output profiles (template columns, date format, defaults, save location, filename pattern, open behaviour). Validation before building: balanced, account and tracking codes exist and are active, date in an open period, required fields present, "Not Given" for missing optional fields.
8. **Stamp and register.** Each file gets an ID and a lifecycle status (built, stamped, posted, rejected). Stamping re-validates, records who and when, and stores a SHA-256 hash; any later edit invalidates the stamp.
9. **Dimension tagging.** Customer-defined dimensions (event, department, venue, campaign and so on), each with its own reference data, such as an event calendar CSV for events or timesheet department codes for labour. Attribution rules by data type with configurable windows, confidence levels, a review queue, manual overrides, and a stale-tag flag when the source record changes. Design decisions (7 October 2026):
   - **Unlimited dimensions.** Xero allows two active tracking categories; iLA allows any number. A dimension may be linked to a Xero tracking category (e.g. Event), and its values to tracking options.
   - **Tag at line or transaction level; store per line.** An *assignment* records a decision for one line, or for a whole invoice, bill, credit note, bank transaction or journal, split across one or more values (shares sum to 100%). *Tags* are the per-line result that reports read, so the P&L by dimension still ties to the cent. A transaction-level assignment also covers lines added later in Xero and does not go stale when lines are edited; a line-level one does.
   - **Groups are dimension values.** Tagging a group (e.g. all invoices and bills for the Smith wedding) creates one transaction-level assignment per transaction with the same value, sharing a batch ID. No separate group object.
   - **Precedence per line:** manual, then file import, then Xero tracking, then rules; at the same level of authority, line beats transaction; then the newest wins. A manual "Unassigned" on one line overrides a transaction-level tag.
   - **iLA may override Xero tracking.** Where a dimension is linked to a tracking category, Xero's tracking comes in as confirmed line-level assignments, but a manual iLA tag wins. Every line where iLA and Xero disagree (including splits, which Xero cannot hold) is listed as a mismatch to correct in Xero via a journal or import file.
   - **When tagging runs (10 October 2026).** Manual tags apply immediately. Attribution rules run only when the user runs `tag --dim <name> --period <period>` (optionally limited to transaction types), never automatically on sync. Sync only re-applies existing decisions to the transactions it changed and refreshes Xero tracking. Reports (`report pl --by <dimension>`) never tag: they read stored tags, show Unassigned and the share resting on unconfirmed proposals, and offer the `tag` command for the period.
   - **Custom attributes.** Each dimension defines its own fields (text, number, date, yes/no; optionally required), e.g. budget and owner for a campaign; each value stores its attributes and is validated against them.
10. **P&L by dimension.** Columns per value of the chosen dimension (each event, or each department) plus Unassigned, with a tie-out row against Xero's P&L for the same period that must show zero difference.
11. **Audit log.** Every command, approval, stamp and override is logged with user, time, input and result.

**Functional: should-have**

1. Plain-English input drafting a command for approval (Claude API).
2. Supplier rules learned from repeated overrides.
3. Saved command scripts for month-end routines.
4. Excel-safe output (XLSX with text-typed code columns) for files opened in Excel.

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
2. **Week 2 (12–18 Oct): console, search and reports.** Command parser, autocomplete and help. `find`, `bal`, `ar ageing`, `show inv`, `report pl`, `report tb`, `export`. Full-text search index. Add-on: an `ila` terminal shell (Python, prompt\_toolkit) calling the same API, with persistent history, up/down arrows, Ctrl+R search, autocomplete, and `?` for plain-English questions that draft a command for approval. The web console stays the focus; the shell is for power users.
3. **Week 3 (19–25 Oct): tracking-category tagging.** Dimension setup (event calendar, department codes), attribution rules for bills, labour and receipts, confidence levels, review queue, overrides, `report pl --by <dimension>` with the tie-out row.
4. **Week 4 (26 Oct–1 Nov): journal files and stamping.** Output profiles, validation rules, file builder, open-after-build, register, `stamp` with hash, `status --pending`, `mark posted`.
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
