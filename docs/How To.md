# iLA How To

A practical guide to the iLA console: what each command is for, how to type it, and when to use it. For the
full list of parameters with examples, see [Command reference](Command%20reference.md), or type `/help <command>` in
the console.

_Last updated: 17 October 2026 (added `/pl --event`)._

---

## 1. Getting started

1. Sign in at https://ila.works and open the **Console** (`/console/`).
2. Type `/orgs` to see your organisations. A `*` marks the one in use.
3. Switch with `/use <name>`, e.g. `/use TECC`.
4. If the organisation isn't connected to Xero yet, or iLA says it needs new permissions, type `/connect`.
5. Type `/status` for a first look at the business.

**Keyboard:** Enter runs a command · ↑ ↓ scroll through your command history · Tab completes a command name ·
`/clean` clears the screen (history is kept).

---

## 2. How commands are written

```
/command [text] [--parameter ...] [<< input file] [>> output file]
```

| Part | What it means | Example |
| --- | --- | --- |
| `/command` | Every native command starts with `/`. Most have short aliases. | `/debtors` or `/ar` |
| `text` | Free text straight after the command: a name, a number, a search. | `/inv INV-0042` |
| `--parameter` | Options, in any order. Values may contain spaces. | `/pl --September 2026 --ytd` |
| `<< file` | An input file. A file picker opens when you run it; the name after `<<` is optional. | `/createTag Event <<` |
| `>> file` | Save the result as Excel (`.xlsx`) or CSV (`.csv`) to your Downloads folder. | `/tb >> tb-sep.xlsx` |

Unknown parameters are errors, never silently ignored, so a typo won't give you a wrong report.

### Periods

Anywhere you see `--<period>`, you can write:

| Kind | Examples |
| --- | --- |
| Month | `--September 2026`, `--Sep 2026`, `--Sep-26`, `--2026-09`, `--09/2026` |
| Relative | `--this month`, `--last month`, `--this quarter`, `--last quarter`, `--this fy`, `--last fy`, `--today`, `--yesterday` |
| Quarter / year | `--Q1 FY2027`, `--FY2026` |
| Date | `--30/06/2026` (day first) |
| Range | `--Jul 2026 to Sep 2026` |

Quarters and financial years follow the year end set in Xero.

### To-date windows

`--mtd`, `--qtd` and `--ytd` mean month, quarter or financial year to date, ending at the period end (or today).

---

## 3. Everyday tasks: which command to use

| I want to… | Type |
| --- | --- |
| Get a start-of-day snapshot | `/status` |
| See revenue for a month against budget and last year | `/status --September 2026 --revenue --mtd` |
| Prepare a P&L for the month-end pack | `/pl --last month >> pl.xlsx` |
| See profit by event | `/pl --event --Sep 2026` (or `/pl --by event`) |
| Get a balance sheet at year end | `/bs --as at 30/06/2026` |
| Get a trial balance for the audit file | `/tb --as at 30/06/2026 >> tb.xlsx` |
| Check bank balances before a payment run | `/cash` |
| Plan the payment run | `/bills --due 14` |
| See only overdue bills | `/bills --overdue` |
| Chase debtors | `/debtors --top 10`, then `/debtors --detail` |
| Review what we owe suppliers | `/creditors --detail` |
| Check bank reconciliation progress | `/recon` |
| Spot unusual or missing supplier bills | `/compare --bill` |
| Look at one supplier over 12 months | `/compare --bill Origin Energy` |
| Watch consultancy spend | `/compare --bill --account Consulting --detail` |
| Check a single bill against its history | `/compare --bill OE-0912` |
| Trend revenue or expenses month by month | `/compare --revenue --expenses >> trend.xlsx` |
| Open an invoice, bill or credit note | `/inv INV-0042` |
| Find a contact or document | `/find smith` |
| See what I can tag transactions by | `/dim`, then `/dim event` |
| Give Xero's event codes (e.g. CCH1221) real names and details | `/createTag Event` |
| Add a new way to tag that Xero doesn't have | `/createTag Venue` |
| Tag March's bills and invoices to events | `/tag --dim event --bills --invoices --Mar 2026`, check, then `approve high` |
| Put everything mentioning ABBA on the ABBA event | `/tag --dim event --all --Mar 2026 --desc ABBA --to 260328ABB` |
| Build a customer invoice import file | `/_createInvoice` (if your organisation has one) |

---

## 4. Native commands

### Snapshot and reports

#### `/status` — the business at a glance
- **Purpose:** cash, revenue, debtors, bills due (including overdue) and unreconciled items; or chosen measures
  against budget and last year.
- **Syntax:** `/status [--<period>] [--revenue|--cos|--gp|--expenses|--profit] [--mtd|--qtd|--ytd]`
- **Use it:** first thing each day. Add a period and a measure to compare, e.g. `/status --FY2026 --profit`.

#### `/pl` (`/p&l`, `/profit`) — profit and loss
- **Purpose:** P&L by account against budget and last year.
- **Syntax:** `/pl [--<period>] [--mtd|--qtd|--ytd] [--by <dimension> | --<dimension>] [>> file.xlsx]`
- **Use it:** defaults to this month to date. `/pl --ytd >> pl-ytd.xlsx` for a pack.
- **By event:** `/pl --event --Sep 2026` (or `/pl --by event`, or any other iLA dimension) gives the P&L
  with a column per event, then **Unassigned**, **Total in iLA**, **Xero P&L** and **Difference**.
  - **Every tag counts once:** Xero's own Event tracking and the tags you approved in `/tag` are one set.
    Where both exist on the same transaction, your iLA tag wins.
  - iLA first reads the period's transactions from Xero, so anything tagged in Xero since is included.
  - Amounts are net of tax, in Australian dollars, shown as Xero shows them (income and costs both
    positive). A bill split across events is divided to the cent.
  - **Difference** is what's in Xero's P&L but not in what iLA reads (invoices, bills, credit notes, bank
    transactions, manual journals): usually depreciation, payroll, expense claims or currency gains. The
    report names the largest. Those amounts can't be split by event yet.
  - Figures at the top show the share of income and costs that's tagged.
  - Add `>> events.xlsx` to save it.

#### `/bs` (`/balancesheet`) — balance sheet
- **Purpose:** balance sheet as at a date (default today).
- **Syntax:** `/bs [--as at <date>] [>> file.xlsx]`
- **Use it:** month or year end: `/bs --as at 30/06/2026`, or `/bs --FY2026` for the year's last day.

#### `/tb` — trial balance
- **Purpose:** trial balance as at a date (default today).
- **Syntax:** `/tb [--as at <date>] [>> file.xlsx]`
- **Use it:** tie-outs and audit files: `/tb --as at 30/09/2026 >> tb-sep.xlsx`.

### Cash, receivables and payables

#### `/cash` (`/bank`) — bank balances
- **Syntax:** `/cash [--as at <date>]`
- **Use it:** a quick check before approving payments.

#### `/debtors` (`/ar`) — receivables ageing
- **Purpose:** who owes what, aged by days past the due date.
- **Syntax:** `/debtors [--detail] [--top <n>] [>> file.xlsx]`
- **Use it:** before chasing calls. `--detail` lists each invoice; `--top 10` shows only the biggest customers.

#### `/creditors` (`/ap`) — payables ageing
- **Purpose:** what we owe suppliers, aged by days past the due date.
- **Syntax:** `/creditors [--detail] [--top <n>] [>> file.xlsx]`

#### `/bills` — bills to pay
- **Purpose:** bills due in the next 7 days, overdue ones first.
- **Syntax:** `/bills [--due <days>] [--overdue] [>> file.xlsx]`
- **Use it:** to plan the payment run. `--due 14` looks two weeks ahead; `--overdue` shows only overdue bills.

#### `/recon` (`/rec`) — bank reconciliation status
- **Purpose:** unreconciled Xero transactions per bank account, and the oldest date.
- **Syntax:** `/recon`
- **Note:** Xero's dashboard counts unreconciled *statement lines*, which can be higher than iLA's figure. iLA
  can't see statement lines yet (Xero restricts that API).

### Analysis

#### `/compare` — month-by-month comparison
- **Purpose:** spot unusual, new or missing amounts in bills, invoices or P&L measures.
- **Syntax:**
  - `/compare --bill|--invoice [<number or name>] [--supplier|--customer <name or code>] [--account <code or name>] [--by account] [--<period>] [--detail] [--min <amount>]`
  - `/compare --revenue|--cos|--gp|--expenses|--profit [--<period>]`
- **Two ways to use it:**
  1. **Scan** (no name): `/compare --bill` checks every supplier for last month against the 12 months before, and
     flags each one **High**, **Low**, **New** or **Missing**. `/compare --invoice` does the same for customers.
     Add `--by account` to scan by account instead.
  2. **One item over time** (with a name, code or number): `/compare --bill Origin Energy` shows that supplier
     month by month for the last 12 months plus this month to date. Give a range (up to 24 months) to look further
     back. A bill number compares that bill with the supplier's 12 months before it.
- **Tips:** `--detail` lists the documents behind each month, and `/inv <number>` opens any of them. `--min 500`
  ignores movements under $500 (default $100).

### Look-ups

#### `/inv` (`/invoice`, `/bill`) — open one document
- **Syntax:** `/inv <number>`
- **Use it:** opens an invoice, bill or credit note with its lines, payments and amount due.

#### `/find` (`/search`) — search
- **Syntax:** `/find <text>`
- **Use it:** searches contacts, and invoice and bill numbers and references, e.g. `/find INV-004`.

### Tagging

#### `/dim` (`/dimensions`, `/dims`) — what you can tag by
- **Purpose:** lists Xero tracking categories and iLA's own dimensions.
- **Syntax:** `/dim [<name>] [--archived]`
- **Use it:** before tagging. Xero allows 2 active tracking categories (4 in total); iLA dimensions have no limit
  and never change Xero. `/dim event` shows a category's options, or a dimension's values with their extra fields
  (e.g. venue, event date). `--archived` includes archived items.

#### `/createTag` — set up a dimension from a values file
- **Purpose:** create a dimension to tag by, or give an existing Xero tracking category friendly names and
  details. Xero codes like `CCH1221` or `240525ISA` mean little on their own; iLA lets you name them (e.g.
  "Celtic Nights 2026") and add details (venue, event date) **without changing Xero**.
- **Syntax:** `/createTag [<name>] [--new] [<< values.csv]`
- **Quickest way:** `/createTag Event <<` picks Xero's Event category, keeps the name and opens the file picker,
  then shows the preview straight away.
- **Steps:**
  1. **What are you setting up?** iLA lists your Xero tracking categories and iLA dimensions. Pick one:
     - **Replace Xero's … tracking:** iLA shows your names and details instead of Xero's codes. Each Xero code stays
       linked, so tags still tie back to Xero.
     - **Update iLA dimension …:** refresh the values of a dimension you already have.
     - **A new dimension:** a way to tag that Xero doesn't have (no limit on how many).

     `/createTag Event` skips this step when the name matches.
  2. **Name:** type a name, or `ok` to keep the one suggested.
  3. **Values file:** download the starting file (when replacing a Xero category it already has every Xero code
     filled in), complete it in Excel, save it as CSV and choose it with `<<`.

     | Column | Required | What goes in it |
     | --- | --- | --- |
     | Code | Yes, or empty | The short code. When replacing a Xero category, the Xero option exactly as in Xero (e.g. `CCH1221`). Leave it empty and iLA makes one: the start date as YYMMDD plus the first three letters of the name (e.g. `261025FIR`), or the name's letters if there's no date (e.g. `ACRRM`). |
     | Name | Yes | The name people should see, e.g. Celtic Nights 2026. |
     | Description | No | Anything that helps people pick the right one. |
     | Active | No | `yes` or `no` (blank means yes). |
     | Any other column | No | Becomes an extra field, e.g. Venue, Event date (`30/06/2026`), Capacity, Ticketed (`yes`/`no`). iLA works out the type from the values. |

  4. **Review:** iLA shows each value as New, Updated or No change, and lists any problems (empty or duplicate
     codes, missing names, wrong types) and warnings (codes not in Xero, Xero codes missing from the file). Fix the
     file and choose it again with `<<` if needed.
  5. **Approve:** type `approve` to save. Nothing is saved before this. Type `cancel` to drop it.
- **Good to know:**
  - Xero codes left out of the file are still added, named by their code, so every Xero code has a value.
  - Codes never repeat within a dimension, ignoring capitals (`ABC` and `abc` count as the same). A duplicate in
    the file is a problem to fix; a made code that's already taken gets a number added (`261025FIR2`). The
    same rule applies on the admin page, and dimension codes are unique within an organisation.
  - If a made code matches a Xero code, iLA links them and says so: check it's the right event.
  - Values already in iLA but not in the file stay as they are, and empty cells don't erase a saved detail.
  - Run `/createTag Event` again whenever you have new events: it updates what's there.
  - The admin **Dimensions** page shows the same set-up, including "Replaces a category in the accounting
    system" and "Name in the accounting system" (e.g. Events Department), and lets you edit single values.

#### `/tag` — tag transactions to a dimension value
- **Purpose:** say which event (or other dimension value) each invoice, bill, credit note, bank transaction or
  manual journal belongs to, so you can report by event. Tags live in iLA; **Xero is never changed**.
- **Syntax:**
  `/tag --dim <dimension> --invoices|--bills|--credits|--bank|--journals|--all --<period> [filters] [--to <value>]`
- **What to tag** (any of them): `--invoices`, `--bills`, `--credits` (sales and supplier credit notes), `--bank`
  (spend and receive money; transfers are skipped), `--journals` (manual journals only), or `--all`.
- **Period:** any period format. **Both end dates are always included.**
- **Filters** (combine any; every one must match):

  | Filter | Keeps |
  | --- | --- |
  | `--desc ABBA` | Description, reference or number contains it. `--desc ABBA or Bjorn` for either. |
  | `--contact Ticketek` | The contact's name contains it. |
  | `--account 310` or `--account Consulting` | Only lines on those accounts. Without it, iLA looks at **P&L lines only**. |
  | `--amount >1000`, `<500`, `500 to 5000` | The P&L amount (for a journal, its debits). |
  | `--xero Event` or `--xero Event 260328ABB` | Only lines Xero already tracks in that category (and option). |
  | `--attachment` | Only transactions with a file attached in Xero. |
  | `--retag` | Also propose (and with `--to`, change) a value for transactions already tagged. Without it they're listed with no proposal, so `approve` leaves them alone. |

- **Tagged to what:**
  - `--to 260328ABB` (code or name) puts every match on that value.
  - Without `--to`, iLA **proposes** a value for each transaction, with a confidence and the reason:

    | Evidence | Confidence |
    | --- | --- |
    | Xero tracking already on the line | High |
    | The value's code in the text, e.g. 260328ABB in the reference | High |
    | The value's name, or a distinctive word of it (e.g. "Mecca" for MECCA Training), in the text **and** the date fits | High |
    | The name or distinctive word in the text, but the date doesn't fit | Medium |
    | Same contact as other transactions of that event, and the date fits (suppliers often work on many events) | Low |

    The date on its own never proposes an event, because overhead bills (power, cleaning) fall inside some
    event's dates. The Why column still says which events' dates fit.

    Generic words such as "training", "night", "graduation" or "formal" don't count on their own. When two events
    share a name (e.g. MECCA Training in August and September), the one with the nearer date is proposed.
    iLA reads each transaction's description, reference and number from Xero; it doesn't open attached files yet.

    "The date fits" means between the event's start and end date, give or take 3 days (`--window 7` to widen).
    When two events are equally likely, iLA proposes neither and says so.
- **Preview, then approve.** Nothing is tagged until you approve:

  | Type | Does |
  | --- | --- |
  | `approve` | Tags every row that has a proposal, **including low ones**. |
  | `approve high` or `approve high medium` | Tags only rows at those confidences. |
  | `approve 1-5,8` | Tags those rows (by the # column). |
  | `skip 3,4` | Leaves those rows out. |
  | `set 3,4 260328ABB` | Chooses the value yourself. |
  | `cancel` | Drops the preview. |

  Rows you don't approve stay untagged; run the same `/tag` again to review them.
- **The table** lists every match, already tagged ones included. **Tagged in Xero** shows Xero's tracking on the
  transaction (e.g. `260910COW`); **Tagged in iLA** shows its current iLA tag. To change an already-tagged one,
  `set` its row and approve it.
- **Where your decisions are saved:** admin → Core → **Assignments** (filter Method = Manual for the
  ones you approved; Tracking for those from Xero), and admin → Console → **Tag runs** for each
  `/tag` preview and how many rows were approved.
- **Good to know:**
  - `/tag` first reads the chosen transactions for the period from Xero (only approved or paid documents, bank
    transactions and posted manual journals). A month takes about 20 seconds the first time and is quicker after.
  - Whole transactions are tagged, so lines Xero adds later are covered too. Tagging single lines or splitting
    one bill across events comes later.
  - Xero tracking (e.g. TECC's Event tracking) becomes tags automatically when transactions are read. A tag you
    approve in `/tag` wins over Xero's tracking for that transaction.
  - `--dim` is always an iLA dimension. Xero's tracking is only read, with `--xero`.
  - Add `>> tagging.xlsx` to save the preview to Excel.

### Organisations and connection

| Command | Purpose | Syntax |
| --- | --- | --- |
| `/orgs` | List your organisations; `*` marks the current one | `/orgs` |
| `/use` | Switch organisation; later commands read that one | `/use <name>` |
| `/connect` | Connect or reconnect Xero, and grant any new permissions iLA needs | `/connect` |

### Screen and help

| Command | Also | Purpose | Syntax |
| --- | --- | --- | --- |
| `/clean` | `/clear`, `/cls` | Clear results on screen (history kept) | `/clean` |
| `/help` | `/?` | List all commands (including your organisation's built ones), or explain one | `/help [command]` |
| `/cancel` | | Drop your open `/tag` preview, `/createTag` or `/_createInvoice` draft; nothing is saved | `/cancel` |

---

## 5. Built commands (names start with `_`)

Built commands belong to one organisation. Superusers (and users with the *build commands* permission) make them on
the **Builder** page (top bar). Run them as `/_name` or just `_name`. A built command can never replace a native
one, because native commands never start with `_`.

`/help` lists your organisation's built commands, and `/help _name` shows what one does.

### Template: Start from scratch (shortcuts)

Runs up to 10 native commands in order and shows them as one report. Good for a month-end pack or a weekly
supplier check.

- Type one command per line, without `<<` or `>>`.
- `{text}` passes on the text typed after the name; `{params}` passes on the `--` parameters.
- `/use`, `/connect` and `/clean` can't be steps, and built commands can't run other built commands.
- Add `>>` when you run it, to save every table: `/_monthend --last month >> pack.xlsx`.

Example `_monthend` steps:

```
/pl {params}
/bs
/debtors --top 10
/creditors --top 10
```

Then run `/_monthend --September 2026`.

### Template: createInvoice

A guided conversation that builds a customer invoice as a Xero import file. Each organisation needs an **invoice
set-up** first (admin → Organisation → Invoice set-ups): its items, revenue accounts, tax basis and tracking category.

**Steps:**

1. **Start:** `/_createInvoice` (or `/_createInvoice <customer>` to jump to the customer search). If there's an
   open draft, iLA picks it up. `--new` starts again.
2. **Set-up:** if the organisation has more than one, type its number.
3. **Customer:** type part of the name; iLA searches Xero. Pick a number, or choose "new customer" (Xero creates the
   contact on import).
4. **Event or job:** pick from recent references or the tracking options, or type a name.
5. **Billing information:** add files with `<<` (several at once: agreements, orders, supplier bills, timesheets,
   settlements), or type notes such as hours and rates. Type `done` or `go ahead` when ready.
6. **Review:** iLA shows the draft with its assumptions and questions. Type changes in plain English
   (e.g. "number INV-2189", "remove the cleaning line"), or add more files with `<<`.
7. **Approve:** type `approve`. iLA checks accounts, tax rates, tracking options and that the invoice number is unused,
   then saves the import file and offers a download link.
8. **Import into Xero:** Sales → Invoices → Import, choose the file and select the tax basis iLA tells you. It imports
   as a **draft** for the usual approval.

**While a draft is open,** anything typed without `/` goes to the draft. Type `cancel` or `/cancel` to drop it.

**Who does what:** the AI reads the documents and proposes lines and questions. iLA itself decides the account
codes and tax rates (from the set-up, past invoices and defaults), calculates every amount and checks the result.
Nothing is written to Xero; a person imports the file.

---

## 6. Troubleshooting

| Message or problem | What to do |
| --- | --- |
| "There's no /x command" | Check the spelling, or type `/help`. |
| "… isn't an option" / unknown parameter | Type `/help <command>` to see its parameters. |
| iLA asks for new permissions, or Xero data won't load | Type `/connect` to reconnect Xero. |
| Results are for the wrong company | Type `/orgs`, then `/use <name>`. |
| `/tag` proposes nothing for most rows | Narrow with `--desc` or `--contact` and use `--to`, or `set` rows yourself. Make sure the dimension's values have start and end dates. |
| Plain text gives an error | Without an open `/tag` preview, `/createTag` or invoice draft, every command must start with `/` (or `_` for built ones). |
| `/_createInvoice` says the model isn't set up | The AI key (`ANTHROPIC_API_KEY`) is missing on the server. Ask the administrator. |
| `/createTag` says the file needs fixing | Read the listed rows, correct the CSV and choose it again with `<<`. |
| `/createTag` file came from Excel and looks garbled | Save it as **CSV UTF-8** (File → Save As). |
| `/_createInvoice` says there's no invoice set-up | Add one in admin → Organisation → Invoice set-ups. |
| Figures look a few minutes old | Xero reads are cached for 5 minutes. |

---

## 7. Coming next

Planned, not built yet:

- `/tag --lines`: tag single lines and split one transaction across events.
- Purchase orders, and reading attached PDFs to find the event.
- `/_createInvoice` from timesheets (day lines, hours × rate, job codes).
- `_checkBill` and `_recFA` templates.
- `--last N months` period syntax.
- Plain-English input: type without `/` and iLA drafts the commands for you to approve.
- Exact bank reconciliation, e.g. `/recon << statement.csv`.

---

## Change log

| Date | Change |
| --- | --- |
| 17 Oct 2026 | Guide created, covering all native commands, built commands and `/dim`. |
| 17 Oct 2026 | Added `/createTag` (replaces the planned `_createTag` template). |
| 17 Oct 2026 | Added `/pl --event` (`/pl --by <dimension>`). |
| 17 Oct 2026 | Added `/tag`. |
| 17 Oct 2026 | `/tag` lists already-tagged transactions too (Tagged in Xero / Tagged in iLA columns); a distinctive word of an event name counts as evidence; "same supplier" is now low confidence. |
| 17 Oct 2026 | `/createTag Event <<` takes the file in one go; a bare `<<` no longer needs a file name. |
| 17 Oct 2026 | `/createTag` makes a code when the Code cell is empty; codes are unique ignoring capitals. |
