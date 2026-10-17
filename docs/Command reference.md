# iLA command reference

Every command starts with `/`. Parameters follow `--` in any order and may contain spaces (`--September 2026`). `<< file` gives an input file (a file picker opens); `>> name.xlsx` or `>> name.csv` saves the result to your Downloads folder. Unknown parameters are errors, never ignored. Type `/help <command>` in the console for the same detail.

**Periods** (wherever `--<period>` appears): `September 2026`, `Sep 2026`, `Sep-26`, `2026-09`, `09/2026`, `this month`, `last month`, `this quarter`, `last quarter`, `Q1 FY2027`, `FY2026`, `this fy`, `last fy`, `today`, `yesterday`, a date such as `30/06/2026` (day first), or a range: `Jul 2026 to Sep 2026`. Quarters and years follow the financial year end set in Xero.

**Built commands** start with `_` (`/_weekly`, or just `_weekly`) and belong to one organisation. Superusers make them on the Builder page (top bar). `/help` lists them and `/help _name` shows the commands they run. They are not listed here because each organisation has its own. A command made from the createInvoice template (e.g. `/_createInvoice [customer] [--new]`) starts a guided conversation; while it is open, plain answers (no `/`) go to it, `<<` adds files (several at once), `approve` builds the import file and `/cancel` drops the draft.

## Summary

| Command | Also | What it does | Usage |
| --- | --- | --- | --- |
| `/status` |  | Quick update: cash, revenue, debtors, bills due and bank reconciliation; or a measure against budget and last year. | `/status [--<period>] [--revenue\|--cos\|--gp\|--expenses\|--profit] [--mtd\|--qtd\|--ytd]` |
| `/pl` | `/p&l`, `/profit` | Profit and loss by account, against budget and last year. | `/pl [--<period>] [--mtd\|--qtd\|--ytd] [>> file.xlsx]` |
| `/bs` | `/balancesheet` | Balance sheet as at a date (default today). | `/bs [--as at <date>] [>> file.xlsx]` |
| `/tb` |  | Trial balance as at a date (default today). | `/tb [--as at <date>] [>> file.xlsx]` |
| `/cash` | `/bank` | Bank account balances as at a date (default today). | `/cash [--as at <date>]` |
| `/debtors` | `/ar` | Receivables ageing by customer (by days past due date). | `/debtors [--detail] [--top <n>] [>> file.xlsx]` |
| `/creditors` | `/ap` | Payables ageing by supplier (by days past due date). | `/creditors [--detail] [--top <n>] [>> file.xlsx]` |
| `/bills` |  | Bills due in the next 7 days (or --due <days>), with overdue bills listed first. | `/bills [--due <days>] [--overdue] [>> file.xlsx]` |
| `/recon` | `/rec` | Bank reconciliation status per bank account. | `/recon` |
| `/compare` |  | Compare bills, invoices or a P&L measure month by month to spot unusual, new or missing amounts. | `/compare --bill\|--invoice [<number>] [--supplier\|--customer <name or code>] [--account <code or name>] [--by account] [--<period>] [--detail] [--min <amount>]   or   /compare --revenue\|--cos\|--gp\|--expenses\|--profit [--<period>]` |
| `/inv` | `/invoice`, `/bill` | Show one invoice, bill or credit note by its number. | `/inv <number>` |
| `/find` | `/search` | Search contacts, and invoices and bills by number or reference. | `/find <text>` |
| `/dim` | `/dimensions`, `/dims` | What you can tag by: Xero tracking categories (2 active, 4 in all) and iLA's own dimensions (no limit). | `/dim [<name>] [--archived]` |
| `/orgs` |  | List your organisations; * marks the one in use. | `/orgs` |
| `/use` |  | Switch to another organisation. | `/use <name>` |
| `/connect` |  | Connect or reconnect Xero (grants any new permissions). | `/connect` |
| `/cancel` |  | Drop your open /_createInvoice draft. Nothing is written anywhere. | `/cancel` |
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

Profit and loss by account, against budget and last year.

Usage: `/pl [--<period>] [--mtd|--qtd|--ytd] [>> file.xlsx]`

| Parameter | Meaning |
| --- | --- |
| `--<period>` | Defaults to this month to date |
| `--mtd --qtd --ytd` | To-date window (pick one) |

Examples:

- `/pl`
- `/pl --September 2026`
- `/pl --ytd >> pl-ytd.xlsx`
- `/pl --Jul 2026 to Sep 2026`

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

### /recon

Bank reconciliation status per bank account.

Usage: `/recon`

Examples:

- `/recon`

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
