# Expectation Ledger — Design

Oct 24, 2026 · @Erin Yin

## Vision

iLA should do the accounting work itself — close the books and reconcile them — while people check, review, question and approve. To do that it has to know what is happening in the business before it reaches the ledger: events held and not yet invoiced, unreconciled bank lines, bills arriving and whether they are legitimate, and costs incurred but not yet billed.

The ledger is the last place information arrives. A human accountant closes the books by carrying a mental model of what *should* have happened this month and checking the ledger against it. The **Expectation Ledger** makes that model explicit.

## What the Expectation Ledger is

A layer, kept in iLA, of **expected economic events** for each period, drawn from operational systems, the source ledger, files and what users tell iLA. Each expectation predicts the entries it should produce (revenue, cost, cash, timing).

- It **never changes the source ledger** (Xero, Sage Intacct). It sits beside it, and the two can always be compared and explained — the same principle as the console's enrichment layer.
- It **keeps updating** as more happens: new transactions in Xero, files fed into iLA, information from users or the outside world.
- It **learns**: every closed period gives an actual to compare against each expectation.
- It **converges** with the real ledger when the month is closed; the remaining difference is the learning signal.
- In the final phase it becomes the **conductor**: the agent that drives the close itself, subject to review.

The close becomes one repeated question: *for every expectation, is there evidence; for every piece of evidence, is there an expectation?*

| Situation | Expectation | Evidence | Result |
| --- | --- | --- | --- |
| Event held, not invoiced | ✓ | ✗ invoice | Raise the invoice or accrue revenue |
| Bill arrives | ? | ✓ | Legitimate only if it has a parent (contract, PO, recurring pattern) |
| Bank line unreconciled | ? | ✓ | Match to open AR / AP / payroll / banking expectation, or ask |
| Service received, no bill | ✓ | ✗ bill | Accrue |

Accruals are simply expectations without a matching bill at cut-off.

### Expectation lifecycle

`predicted → evidenced → posted → closed`

Example: *predicted* "Electricity for September ≈ $18k (24 months' history, seasonality)" → *evidenced* by an early-October bill covering 1–30 Sep, $19.2k → *posted* as a September accrual, auto-reversed 1 Oct → *closed* when the October bill posts; the variance updates the cost profile.

Every state change records who or what changed it and why.

## Where expectations come from (full scope)

- **Revenue** — event and booking systems (event system, Opera, Bepoz, CMS/Cage) say what was delivered. Event held with no invoice → uninvoiced revenue. Deposits before the event date → income in advance.
- **Bills** — agreement-first: every bill should trace to a contract, PO or established recurring supplier. Anomaly checks: new supplier, changed bank details, amount outside historical range, duplicate number or amount, ABN not active, GST not reconciling, odd service period. A bill with no parent goes to a person with the question "who ordered this?"
- **Bank** — match to open AR, approved bills, pay runs, gaming banking, known direct debits. Remaining lines: propose a classification from counterparty history and ask; every answer becomes a rule.
- **People** — a pre-close check-in (Teams bot) asks each venue or department owner: "Here's what I expect for your area this month. Anything missing or wrong?"

## Phase 1: accruals

Accruals are the purest form of the Expectation Ledger: a prediction with no evidence yet, tested against reality a few weeks later. The accrual process is broadly the same in every business: understand the usual recurring costs, look for bills after cut-off that belong to the closing month, and accrue payroll — all learned from historical transactions.

### Engine 1: cost behaviour profiler

Builds a profile per **supplier × account × location/department** from 24+ months of bills and payments.

| Behaviour | Example | How to accrue |
| --- | --- | --- |
| Fixed recurring | Subscriptions, leases, contract cleaning | Exact amount |
| Variable recurring | Electricity, water, gas | Run-rate × seasonality |
| Driver-linked | Security per event, laundry per room-night, casual labour per cover | Driver volume × learned rate |
| Periodic, non-monthly | Quarterly rates, annual insurance | Prepayment / amortisation, not accrual |
| One-off | Repairs, consultants | Only from evidence (cut-off scan, PO, user input) |

The key feature is **billing lag**: days from the end of the service period to the bill date. It tells iLA whether a missing bill at close is normal ("always arrives on the 12th") or suspicious ("this supplier is always on time").

**Built (24 Oct 2026): history scan.** Admin → Accounting → Accruals holds each organisation's set-up (accrual account, e.g. TECC 2000; months of history; materiality; driver tracking categories). The action *Copy history from Xero, then scan* (or `manage.py scan_accruals TECC --copy`) runs in the background, since copying a year from Xero takes a minute or more (TECC, 12 months: 69 seconds) and the web server stops requests after 30 seconds; the Accruals list's *Last job* column shows its progress and result, and one job runs per organisation at a time. It reads every journal on the accrual account, takes the cost lines as what was accrued, groups them by supplier or description × cost account × location tracking (event tracking is the driver, not part of the key), and sorts each profile into the five sections above. Each profile gets a memo (months accrued, amounts, seasonality, the bill that followed and its lag, how close the accrual was, whether it was reversed) and a rule (as text and data). Code: `accounting/scan.py`.

Everything is editable in admin. The thresholds the scan uses (fixed if variation < 5%, recurring if accrued in ≥ 60% of months, bill match tolerance 35%, bill window 75 days, not recent after 6 months, driver-linked from 3 options, run-rate months, driver and ignored words) are fields on the set-up. Each profile's rule is plain fields (method, amount, account, tracking, supplier, driver, driver category, rate, seasonality, reverse, skip if the bill is posted), and the memo is editable too. A field a person changes is listed in *Your changes* and a scan leaves it alone (the action *Let the next scan relearn* clears the list). A deleted profile's key goes on the set-up's *Deleted profiles* list and a scan doesn't bring it back; scans never delete profiles, they mark old ones not recent.

**Built (24 Oct 2026): accrual runs in the Expectation Ledger.** Each run (admin: *Run accruals for last month*, or *Add Expectation Ledger journal* for any month; `manage.py run_accruals TECC --period 2026-09`) is saved as a journal whose lines are `Expectation`s. Lines start **predicted** from the rule; profiles switched off, below materiality, set to prepayment or don't-accrue, or whose month's bill is already in are listed under *Left out of this run* with the reason. *Check for bills* moves a predicted line to **evidenced** when the bill for the month arrives after month end (the line takes the bill amount). *Build and stamp the journal import file* writes a Xero manual journal CSV (Dr cost / Cr 2000 at month end, and the reversal on day 1) into the Import files register, and *Mark posted in Xero* moves open lines to **posted**. *Check for bills* again moves a posted line to **closed** with the actual and the variance, and adds the outcome to the profile, whose accuracy is then the average of its last 12 outcomes. People can add, change or delete lines and change a line's state in admin; every change is in the line's history with who, when and why, deletions are noted on the journal, and a re-run keeps lines a person touched. Code: `accounting/ledger.py`.

**Changed (25 Oct 2026): judged from the pattern, evidence first, its own view.** Tried on TECC's real 2000 (12 months), the first scan made 111 profiles, 105 of them "one-off", because it grouped by the wording of each month's narration ("Electricity Charges Based", "Electricity Expense As" … were nine profiles) and guessed suppliers from words (a law firm on electricity). September came out at $732.86 against the $31,210.80 TECC accrued. Now:

- **Grouping:** one profile per cost account × location/department, however the journals are worded. Payroll (salaries, leave, super, payroll tax, Workcover, bonus) is a separate *Payroll* section, left to the payroll accrual (not built yet); revenue moved through the accrual account is left out. TECC: 56 profiles, one of them *CS Energy — 6034 Electricity, variable recurring, a bill behind 11 of 12 months, 4 days after month end*.
- **Evidence chain:** the invoice a journal says it is based on ("as per Invoice T1171810") is looked up among the bills; that gives the supplier, the bill amount and the lag (34 of TECC's 40 invoice references matched). Then a bill after month end close to the amount; a supplier named in the words only when no bill says who it was. A supplier is the profile's when it is behind at least 60% of the months.
- **Judgement:** from the months since the cost first appeared (accrued in ≥ the recurring threshold of them: recurring; amounts steady within the fixed threshold: fixed, else variable; regular gaps of 2+ months: periodic; event-tagged: driver-linked; else one-off). The memo ends with the judgement, e.g. "variable recurring; accrue the bill for the month when it is in by the close, otherwise the average of the last 3 months before it".
- **Run, evidence first:** a bill dated after month end on the profile's account, from a supplier this cost has been accrued from before, within the cost's longest lag + 7 days, is the accrual (one bill per supplier: the first; a second is usually next month's). A bill line tagged to an event is placed by the event's date (from the Event dimension's start date, else a YYMMDD code such as 261008MEL): held by month end, it is the month's whenever it came; held after, it isn't. A bill from a supplier never accrued on that account, or an untagged bill on an event-driven cost, is listed *to review*, not accrued. For a cost billed once a month, the month's own bill already posted in the month means nothing to accrue; otherwise "already billed" only stops an estimate.
- **Independent of what is posted:** with no bill, the amount comes from months *before* the one being accrued (median, average of the last N months, seasonal index from 12+ earlier months, lag window from earlier months), so the month's own accrual in Xero never feeds its expectation. Xero's accrual journals are only compared afterwards.
- **Compared with Xero:** each journal shows expected vs accrued in Xero by account. TECC September: 43,816.28 vs 31,210.80; bill-based lines match TECC exactly (electricity 25,061.42, fire 1,374.40, IT 1,739.23, AV 640.00, electrical 1,944.00); the difference is the security estimate (4 events × $2,826, no bill), the music licence (usual amount, no bill), a council waste bill, and five items under the $500 floor.
- **Open accruals:** each journal lists every accrual still open in Xero at the month end (reversals matched by narration and amount, bills coded to the accrual account matched by invoice number or supplier), with whether its invoice is in (then it should have been reversed) or not, and debits that matched nothing. TECC: COGS (Bepoz) accrued Mar 2026 and annual leave May–Jun 2026 never reversed; 1 Oct 2025 reversals of accruals from before the copied history need a longer history to pair up.
- **Admin:** the accrual page has a bar of links to each section with its count and ↑ Top on each heading.

Next: Claude reviews what the rules can't place (an untagged bill like Wild Gravity 3869, narrations with no invoice, the memo's wording), proposing for a person to accept; payroll accruals from pay runs.


### Engine 2: cut-off scanner

For the September close, scan everything arriving from 1 October to the close date.

- **Bills dated October** — determine the service period, in order of trust: explicit period on the invoice ("1–30 Sep", "September usage") → delivery/receipt date or PO → line description → supplier's historical lag. Split bills spanning months pro rata.
- **Bills dated September but entered in October** — late postings, not accruals; flag to post into September if the period is open.
- **Credit notes and supplier statements** where those feeds exist.

Every match carries its evidence chain.

### Engine 3: payroll accrual

- **Wages** — days between last pay period end and month end. Use UKG timesheets (actual hours × rates) where available; otherwise run-rate from the last two or three pay runs, pro-rated by working days or roster.
- **On-costs** — superannuation, payroll tax and mental health levy, workers' compensation.
- Leave provisions, bonuses and commissions: phase 2.

### Gap-fill logic

For every profile expected to have activity in the period:

1. A bill for this service period is already posted → nothing to do.
2. Found in the cut-off scan → accrue the actual bill amount (high confidence).
3. Not found, lag says it's still coming → accrue the profile estimate (medium confidence).
4. Not found, should have arrived → exception: "Is this supplier still active?"

Controls:

- Dedupe against manual accruals already in the ledger and against PO receipts.
- Materiality floor (configurable, e.g. $500).
- Auto-reversal on day 1 of the next month.
- Output as a journal import file (Dr expense / Cr accrued expenses, location and department from the profile), stamped by a person before posting — same flow as `/jnl`.

### Learning loop

After each close, compare every accrual with the bill that eventually arrived; update the profile's accuracy and confidence. Reviewer corrections become rules ("this supplier bills quarterly in arrears").

Autonomy is earned **per profile**, not per process: a profile within 2% for six months can post with a sample review; a new or noisy profile always waits for a person.

## Data model (first cut)

```python
@dataclass
class CostProfile:
    supplier_id: str
    acct_no: str
    location_id: str
    dept_id: str
    behaviour: Literal["fixed", "variable", "driver", "periodic", "one_off"]
    cadence_days: float             # median gap between bills
    billing_lag_days: float         # service period end → bill date
    lag_std: float
    amount_model: dict              # mean/median, seasonality index, or driver rate
    driver: str | None              # e.g. "tecc_events", "room_nights"
    accuracy_mape: float            # updated after each close
    confidence: float

@dataclass
class Expectation:
    id: str
    period: str                     # "2026-09"
    profile_id: str | None          # None for user-supplied / one-off
    kind: Literal["accrual", "prepayment", "revenue", "payroll"]
    expected_amount: Decimal
    state: Literal["predicted", "evidenced", "posted", "closed"]
    evidence: list[Evidence]        # bills, timesheets, user notes, files
    confidence: float
    history: list[StateChange]      # who/what changed it and why
    actual_amount: Decimal | None   # filled when settled → learning signal
```

These become Django models in their own app once the shape is proven.

## Architecture fit

adapters (Xero, Sage Intacct, Opera, Bepoz, UKG, event system, bank, Cage) → canonical event model → **expectation engine** → matching → exceptions and review → posting (stamped import file).

Two rules:

1. **Every proposed entry carries its evidence chain**, so review is fast and the books are defensible to auditors.
2. **Autonomy is earned**, gated by confidence and materiality thresholds.

## First build: back-test

1. Load a line-level bills export for one entity (~24 months: supplier, date, account, tracking, description) plus pay run history.
2. Build the profiler.
3. Pretend it is the September 2025 close; let iLA produce accruals.
4. Compare with what actually arrived in October–November 2025.

Measures: accrual accuracy, % of bills with a parent, % of bank lines auto-explained (later phases). This gives a baseline before anyone relies on it.

## Open questions

- Where is the line-level bill history easiest to get — Xero sync already in `xero/`, or a Sage Intacct export?
- Which entity first? TECC is event-driven and self-contained.
- Close calendar: on which working day does the cut-off scan stop?
