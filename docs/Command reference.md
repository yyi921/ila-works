# iLA command reference

Every command starts with `/`. Parameters follow `--` in any order and may contain spaces (`--September 2026`). `<< file` gives an input file (a file picker opens; the name is optional); `>> name.xlsx` or `>> name.csv` saves the result to your Downloads folder. Unknown parameters are errors, never ignored. Type `/help <command>` in the console for the same detail.

**Periods** (wherever `--<period>` appears): `September 2026`, `Sep 2026`, `Sep-26`, `2026-09`, `09/2026`, `this month`, `last month`, `this quarter`, `last quarter`, `Q1 FY2027`, `FY2026`, `this fy`, `last fy`, `today`, `yesterday`, a date such as `30/06/2026` (day first), or a range: `Jul 2026 to Sep 2026`. Quarters and years follow the financial year end set in Xero.

**Built commands** start with `_` (`/_weekly`, or just `_weekly`) and belong to one organisation. Superusers make them on the Builder page (top bar). `/help` lists them and `/help _name` shows the commands they run. They are not listed here because each organisation has its own. A command made from the createInvoice template (e.g. `/_createInvoice [customer] [--new]`) starts a guided conversation; while it is open, plain answers (no `/`) go to it, `<<` adds files (several at once), `approve` builds the import file and `/cancel` drops the draft.

## Summary

| Command | Also | What it does | Usage |
| --- | --- | --- | --- |
| `/status` |  | Quick update: cash, revenue, debtors, bills due and bank reconciliation; or a measure against budget and last year. | `/status [--<period>] [--revenue\|--cos\|--gp\|--expenses\|--profit] [--mtd\|--qtd\|--ytd]` |
| `/pl` | `/p&l`, `/profit` | Profit and loss by account, against budget and last year; or by event (any iLA dimension) with Unassigned and a check against Xero. | `/pl [--<period>] [--mtd\|--qtd\|--ytd] [--by <dimension> \| --<dimension>] [>> file.xlsx]` |
| `/bs` | `/balancesheet` | Balance sheet as at a date (default today). | `/bs [--as at <date>] [>> file.xlsx]` |
| `/tb` |  | Trial balance as at a date (default today). | `/tb [--as at <date>] [>> file.xlsx]` |
| `/cash` | `/bank` | Bank account balances as at a date (default today). | `/cash [--as at <date>]` |
| `/debtors` | `/ar` | Receivables ageing by customer (by days past due date). | `/debtors [--detail] [--top <n>] [>> file.xlsx]` |
| `/creditors` | `/ap` | Payables ageing by supplier (by days past due date). | `/creditors [--detail] [--top <n>] [>> file.xlsx]` |
| `/bills` |  | Bills due in the next 7 days (or --due <days>), with overdue bills listed first. | `/bills [--due <days>] [--overdue] [>> file.xlsx]` |
| `/accrue` |  | Month-end accrual review: proposed accruals by source with their evidence; accept, adjust or reject each, then build the journal file. | `/accrue [--<period>] [--read \| --refresh]` |
| `/reconcile` | `/recon`, `/rec` | Month-end balance sheet reconciliation: each account's Xero balance against its support, with reconciling items, schedules and preparer/reviewer sign-off. | `/reconcile [<account>] [--<period>] [<< schedule.csv] \| /reconcile --bank \| /reconcile --template` |
| `/compare` |  | Compare bills, invoices or a P&L measure month by month to spot unusual, new or missing amounts. | `/compare --bill\|--invoice [<number>] [--supplier\|--customer <name or code>] [--account <code or name>] [--by account] [--<period>] [--detail] [--min <amount>]   or   /compare --revenue\|--cos\|--gp\|--expenses\|--profit [--<period>]` |
| `/inv` | `/invoice`, `/bill` | Show one invoice, bill or credit note by its number. | `/inv <number>` |
| `/find` | `/search` | Search contacts, and invoices and bills by number or reference. | `/find <text>` |
| `/dim` | `/dimensions`, `/dims` | What you can tag by: Xero tracking categories (2 active, 4 in all) and iLA's own dimensions (no limit). | `/dim [<name>] [--archived]` |
| `/tag` |  | Tag invoices, bills, credit notes, bank transactions or journals to a value of an iLA dimension, e.g. events. Preview first; nothing is tagged until you approve. | `/tag --dim <dimension> --invoices\|--bills\|--credits\|--bank\|--journals\|--all --<period> [--desc <text>] [--contact <name>] [--account <code or name>] [--amount <range>] [--xero <category> [<option>]] [--attachment] [--retag] [--to <value>] [--window <days>]` |
| `/splittag` |  | Split the amounts on chosen GL accounts across a dimension's values (e.g. labour across events) by fixed amounts, percentages or both, from a CSV of named rules or a saved rule. | `/splittag [<< rules.csv] [--<rule name>\|--rule <name>] [--<period>] [--gl <accounts>] [--invoices --bills --credits --bank --journals\|--all] [--contact <name>] [--desc <text>] [--template]` |
| `/createTag` |  | Set up a dimension to tag by from a CSV of its values: brand new, or replacing a Xero tracking category so iLA shows names instead of codes. | `/createTag [<name>] [--new] [<< values.csv]` |
| `/orgs` |  | List your organisations; * marks the one in use. | `/orgs` |
| `/use` |  | Switch to another organisation. | `/use <name>` |
| `/connect` |  | Connect or reconnect Xero (grants any new permissions). | `/connect` |
| `/cancel` |  | Drop your open /splittag or /tag preview, /createTag or /_createInvoice draft, or close a /reconcile working paper. Nothing is saved. | `/cancel` |
| `/clean` | `/clear`, `/cls` | Clear the results on screen. Your command history (up arrow) is kept. | `/clean` |
| `/help` | `/?` | List commands, or show how to use one: /help status. | `/help [command]` |

## Parameters and examples

### /status

Quick update: cash, revenue, debtors, bills due and bank reconciliation; or a measure against budget and last year.

Usage: `/status [--<period>] [--revenue|--cos|--gp|--expenses|--profit] [--mtd|--qtd|--ytd]`

| Parameter | Meaning |
| --- | --- |
| `--<period>` | September 2026, Sep 2026, 2026-09, last month, Q1 FY2027, FY2026, Jul 2026 to Sep 2026 |
| `--revenue --cos --gp --expenses --profit` | Measures to show (all four main ones if none given) |
| `--mtd --qtd --ytd` | Month, quarter or financial year to date, ending at the period end (or today) |

Examples:

- `/status`
- `/status --September 2026 --revenue --mtd`
- `/status --revenue --mtd --ytd`
- `/status --FY2026 --profit`

### /pl

Profit and loss by account, against budget and last year; or by event (any iLA dimension) with Unassigned and a check against Xero.

Usage: `/pl [--<period>] [--mtd|--qtd|--ytd] [--by <dimension> | --<dimension>] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--<period>` | Defaults to this month to date |
| `--mtd --qtd --ytd` | To-date window (pick one) |
| `--by <dimension>  or  --<dimension>` | A column per value, e.g. --by event or just --event. Xero tracking and iLA tags count together. |

Examples:

- `/pl`
- `/pl --September 2026`
- `/pl --ytd >> pl-ytd.xlsx`
- `/pl --Jul 2026 to Sep 2026`
- `/pl --event --Sep 2026`
- `/pl --by event --FY2027 >> events.xlsx`

### /bs

Balance sheet as at a date (default today).

Usage: `/bs [--as at <date>] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--as at <date>` | 30/06/2026, 30 Jun 2026, FY2026 (its last day), last month (its last day) |

Examples:

- `/bs`
- `/bs --as at 30/06/2026`
- `/bs --FY2026 >> bs-fy26.xlsx`

### /tb

Trial balance as at a date (default today).

Usage: `/tb [--as at <date>] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--as at <date>` | 30/06/2026, 30 Jun 2026, FY2026, last month |

Examples:

- `/tb`
- `/tb --as at 30/09/2026 >> tb-sep.xlsx`

### /cash

Bank account balances as at a date (default today).

Usage: `/cash [--as at <date>]`

| Parameter | Meaning |
| --- | --- |
| `--as at <date>` | 30/06/2026, last month |

Examples:

- `/cash`
- `/cash --as at 30/09/2026`

### /debtors

Receivables ageing by customer (by days past due date).

Usage: `/debtors [--detail] [--top <n>] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--detail` | List each invoice instead of totals per customer |
| `--top <n>` | Only the n largest customers |

Examples:

- `/debtors`
- `/debtors --top 10`
- `/debtors --detail >> debtors.xlsx`

### /creditors

Payables ageing by supplier (by days past due date).

Usage: `/creditors [--detail] [--top <n>] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--detail` | List each bill instead of totals per supplier |
| `--top <n>` | Only the n largest suppliers |

Examples:

- `/creditors`
- `/creditors --detail >> creditors.xlsx`

### /bills

Bills due in the next 7 days (or --due <days>), with overdue bills listed first.

Usage: `/bills [--due <days>] [--overdue] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--due <days>` | Look ahead this many days (default 7) |
| `--overdue` | Only bills already overdue |

Examples:

- `/bills`
- `/bills --due 14`
- `/bills --overdue >> overdue-bills.csv`

### /accrue

Month-end accrual review: proposed accruals by source with their evidence; accept, adjust or reject each, then build the journal file.

Usage: `/accrue [--<period>] [--read | --refresh]`

| Parameter | Meaning |
| --- | --- |
| `--<period>` | The month, e.g. Sep 2026; default last month |
| `--read` | Read the PDFs of bills that came after month end (in the background) |
| `--refresh` | Work the proposals out again; decided lines are kept |

Examples:

- `/accrue`
- `/accrue --Sep 2026`
- `/accrue --read --Sep 2026`
- `/accrue --refresh`

In an open review, type `accept <lines|all|bills|estimates>`, `adjust <line> <amount> <reason>`, `reject <line> <reason>`, `undo <line>`, `why <line>`, `read`, `refresh`, `compare`, `build` or `done`.

### /reconcile

Month-end balance sheet reconciliation: each account's Xero balance against its support, with reconciling items, schedules and preparer/reviewer sign-off.

Usage: `/reconcile [<account>] [--<period>] [<< schedule.csv] | /reconcile --bank | /reconcile --template`

| Parameter | Meaning |
| --- | --- |
| `<account>` | Open one account's working paper by code or name, e.g. 090 |
| `--<period>` | The month to reconcile, e.g. Sep 2026 (its last day is the period end); default last month |
| `<< schedule.csv` | The schedule supporting the account (or, with an Account column, several accounts) |
| `--template` | Download the schedule template and see how to fill it in |
| `--bank` | Bank accounts: Xero transactions not yet reconciled, today |

Examples:

- `/reconcile`
- `/reconcile --Sep 2026`
- `/reconcile 090 --Sep 2026`
- `/reconcile --template`
- `/reconcile 1400 --Sep 2026 << prepayments.csv`
- `/reconcile --Sep 2026 << schedules.csv`
- `/reconcile --bank`
- `/reconcile --Sep 2026 >> reconciliations.xlsx`

Also: `/recon`, `/rec`

In an open working paper, type `statement <amount>` or `support <amount>`, `<< schedule.csv`, `add [date] <amount> <description>`, `remove <n>`, `note <text>`, `prepared`, `reviewed`, `reopen`, `next` or `done`.

### /compare

Compare bills, invoices or a P&L measure month by month to spot unusual, new or missing amounts.

Usage: `/compare --bill|--invoice [<number>] [--supplier|--customer <name or code>] [--account <code or name>] [--by account] [--<period>] [--detail] [--min <amount>]   or   /compare --revenue|--cos|--gp|--expenses|--profit [--<period>]`

| Parameter | Meaning |
| --- | --- |
| `--bill  --invoice` | On their own: scan every supplier (or customer) for last month against the 12 months before it |
| `--bill <number or supplier>` | A bill number: that bill against the supplier's 12 months to it. Otherwise a supplier name (or part of it) or code, month by month |
| `--supplier <name or code>` | One supplier month by month (name can be part of it); --customer for sales |
| `--account <code or name>` | Only lines coded to matching accounts, e.g. --account Consulting |
| `--by account` | Scan by account instead of by supplier or customer |
| `--<period>` | Scan: the month to check (default last month). Month by month: the range (default the last 12 months plus this month to date) |
| `--detail` | Also list the documents behind each month |
| `--min <amount>` | Ignore movements smaller than this (default 100) |
| `--revenue --cos --gp --expenses --profit` | Compare P&L measures month by month instead |

Examples:

- `/compare --bill`
- `/compare --bill --by account`
- `/compare --bill Origin Energy`
- `/compare --bill --account Consulting --detail`
- `/compare --bill OE-0912`
- `/compare --invoice --customer Smith`
- `/compare --expenses --gp >> trend.xlsx`

### /inv

Show one invoice, bill or credit note by its number.

Usage: `/inv <number>`

Examples:

- `/inv INV-0042`
- `/inv CN-0007`

### /find

Search contacts, and invoices and bills by number or reference.

Usage: `/find <text>`

Examples:

- `/find acme`
- `/find INV-004`

### /dim

What you can tag by: Xero tracking categories (2 active, 4 in all) and iLA's own dimensions (no limit).

Usage: `/dim [<name>] [--archived]`

| Parameter | Meaning |
| --- | --- |
| `<name>` | Show one tracking category or dimension with its options or values. |
| `--archived` | Include archived categories, options, dimensions and values. |

Examples:

- `/dim`
- `/dim event`
- `/dim department --archived`

### /tag

Tag invoices, bills, credit notes, bank transactions or journals to a value of an iLA dimension, e.g. events. Preview first; nothing is tagged until you approve.

Usage: `/tag --dim <dimension> --invoices|--bills|--credits|--bank|--journals|--all --<period> [--desc <text>] [--contact <name>] [--account <code or name>] [--amount <range>] [--xero <category> [<option>]] [--attachment] [--retag] [--to <value>] [--window <days>]`

| Parameter | Meaning |
| --- | --- |
| `--dim <dimension>` | The iLA dimension to tag to (not needed if you have only one). |
| `--invoices --bills --credits --bank --journals` | What to tag; any of them. --all for every type. Journals are manual journals. |
| `--<period>` | Mar 2026, last month, Jul 2026 to Sep 2026. Both end dates are included. |
| `--desc <text>` | Description, reference or number contains it; ABBA or Bjorn for either. |
| `--contact <name>` | The contact's name contains it. |
| `--account <code or name>` | Only lines on these accounts (default: P&L lines). |
| `--amount <range>` | >1000, <500, 500 to 5000 or 1200 (the P&L amount). |
| `--xero <category> [<option>]` | Only lines Xero already tracks in that category (and option). |
| `--attachment` | Only transactions with a file attached in Xero. |
| `--retag` | Include transactions already tagged in the dimension. |
| `--to <value>` | Tag every match to this value (code or name) instead of proposals. |
| `--window <days>` | How far either side of a value's dates still counts as fitting (default 3). |

Examples:

- `/tag --dim event --bills --invoices --Mar 2026`
- `/tag --dim event --all --Jul 2026 to Sep 2026 --desc ABBA --to 260328ABB`
- `/tag --dim event --bills --last month --contact Ticketek --amount >500`
- `/tag --dim event --journals --FY2026 --xero Event --retag`

### /splittag

Split the amounts on chosen GL accounts across a dimension's values (e.g. labour across events) by fixed amounts, percentages or both, from a CSV of named rules or a saved rule.

Usage: `/splittag [<< rules.csv] [--<rule name>|--rule <name>] [--<period>] [--gl <accounts>] [--invoices --bills --credits --bank --journals|--all] [--contact <name>] [--desc <text>] [--template]`

| Parameter | Meaning |
| --- | --- |
| `<< rules.csv` | Rules to apply; approving also saves them by name. /splittag --template for the file. |
| `--<rule name>  or  --rule <name>` | Run a saved rule (several: --rule labour, casuals). |
| `--<period>` | Sep 2026, last month, Jul 2026 to Sep 2026; both dates included. Needed unless the file has From and To. |
| `--gl <accounts>` | Change the accounts for this run: 6300-6380, 6355 or 6300-6380; 6400. |
| `--invoices --bills --credits --bank --journals --all` | Change the transaction types for this run. |
| `--contact <name>  --desc <text>` | Only lines whose contact or description contains it. |
| `--template` | Download the CSV template. |

Examples:

- `/splittag`
- `/splittag --template`
- `/splittag --Sep 2026 << labour.csv`
- `/splittag --labour --Sep 2026`
- `/splittag --gl 6300-6380 --sep-2026 --labour`
- `/splittag --rule casuals --Oct 2026 --journals`

### /createTag

Set up a dimension to tag by from a CSV of its values: brand new, or replacing a Xero tracking category so iLA shows names instead of codes.

Usage: `/createTag [<name>] [--new] [<< values.csv]`

| Parameter | Meaning |
| --- | --- |
| `<name>` | The Xero tracking category or iLA dimension to set up, or the name of a new one. |
| `--new` | Drop the open /createTag draft and start again. |
| `<< values.csv` | The values file. With a name, iLA keeps that name and goes straight to the preview. |

Examples:

- `/createTag`
- `/createTag Event`
- `/createTag Event << events.csv`
- `/createTag Venue`

### /orgs

List your organisations; * marks the one in use.

Usage: `/orgs`

Examples:

- `/orgs`

### /use

Switch to another organisation.

Usage: `/use <name>`

Examples:

- `/use Demo Company`

### /connect

Connect or reconnect Xero (grants any new permissions).

Usage: `/connect`

Examples:

- `/connect`

### /clean

Clear the results on screen. Your command history (up arrow) is kept.

Usage: `/clean`

Examples:

- `/clean`

### /help

List commands, or show how to use one: /help status.

Usage: `/help [command]`

Examples:

- `/help`
- `/help status`
