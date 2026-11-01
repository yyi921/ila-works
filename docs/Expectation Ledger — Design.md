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

Accruals are the purest form of the Expectation Ledger: a prediction with no evidence yet, tested against reality a few weeks later. The accrual process is broadly the same in every business: understand the usual recurring costs, look for bills after cut-off that belong to the closing month, and accrue payroll — all learned from historical transactions and knowledge of the business. 

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

However, iLA should also be "vigilant" around changes in the business, such as say we swap one phone provider with another one, then iLA should know also the existing recurring accrual is going to change due to the new provider might have a new billing cycle or a new amount or even fee free period. As a general rule, whenever there is a change in business decision, there are GL/Department/Location (if applicable) going to be affected, the accounting profiles attached with these permutations will need to be examined and any impact determined accordingly.

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


### The month-end accrual close: agreed plan (25 Oct 2026)

Goal: the accrual works like an experienced accountant's, within the business. At month end the accrual balance is a
**list of items, each with a reason and evidence**, built from five sources in order of trust, judged, reviewed,
posted, and reconciled to that list.

| # | Source | The question | Evidence |
| --- | --- | --- | --- |
| 1 | Last month rolled forward | What did I accrue? Has its bill arrived? Was it reversed? | Last month's schedule, bills, reversals |
| 2 | Bills after month end | Which bills received since cut-off are for work done by month end? | The bill and its PDF (service period, event date) |
| 3 | What the business did | Which events were held, which costs do they incur, which are unbilled? | Event calendar, recovery revenue, the event's history |
| 4 | What normally happens | Which recurring costs aren't billed yet? | Average of the last N months, billing lag |
| 5 | Reasonableness | Does each cost account look right against its usual level? A big drop is usually a missing bill. | Cost by account vs trailing average |

Decisions (25 Oct 2026):

- **Bill PDFs:** read with the Xero attachments permission (`accounting.attachments.read`, one reconnect per organisation),
  **close window only**: bills dated after month end up to the close, on accounts that are accrued. Each PDF is read
  once and kept with its SHA-256. Claude extracts the service period, event date(s) and name, venue, lines with
  dates and PO, each with the words and page it came from; code decides the month (service ended by month end: the
  month's; spans months: pro-rated; event held in the month: that event's). A PDF that disagrees with Xero's tags is
  flagged, not guessed.
- **Recoveries are mostly billed at cost:** a recovery line on an event's sales invoice (4020 Equipment Hire, 4063 First
  Aid, 4064 Security, 4062 Cleaning, 4061 Wages recovered) means the paired cost was incurred, and the recovery amount
  is the expected cost. Pairs are proposed from account names and history and confirmed in admin.
- **Workspace:** a console command, `/accrue --Sep 2026`, like `/reconcile`: proposals by source with evidence, accept /
  adjust / reject with a reason, then build the journal file. Admin keeps the settings.
- **Order:** A then B, then C, then D.

Each schedule line carries its **source** (subsequent bill, recurring estimate, event expectation, payroll, brought
forward, manual), its **evidence**, the **decision** (accept, adjust, reject; reason, who, when) and the **reversal**
(day 1, or carried until the bill arrives). Approved lines build the Xero journal file (stamped; a person imports it).
`/reconcile 2000` takes the approved schedule plus last month's items still owed as the accrual account's support; what
Xero has that the schedule doesn't (a manual accrual, an old accrual never reversed) is a reconciling item with an
action. Decisions and outcomes teach the profiles. The AI reads PDFs, proposes what rules can't place and explains why;
code computes every amount; nothing changes without approval; iLA never posts.

**Phase A: evidence and approvals.**
1. Attachments: the permission; list and download a bill's attachments; read close-window PDFs with Claude into a
   stored reading (bill, file, SHA-256, extracted facts with quotes, model, when); placement rules in code; the
   reading shown with the bill's evidence on each line. A PDF is never stored by iLA, only what was read from it.
2. Copy sales invoices with the accrual history (needed for recoveries and the event calendar).
3. Decisions on each proposed line: accept, adjust (amount, account, reason) or reject (reason); who and when; only
   accepted and adjusted lines go into the journal file.
4. `/accrue [--<period>]`: run or refresh the month's proposals and show them by source (with "to review" first);
   plain answers in the open review: `accept 1-5`, `accept evidenced`, `adjust 3 1,200 <reason>`, `reject 4 <reason>`,
   `why 3`, `build` (journal file), `done`.

**Phase A built (25 Oct 2026).** `accounting/reading.py` (PDF facts with quotes; placement rules in code; `BillReading` kept by SHA-256), the reading job (`jobs.read_bills`), PDF placement used first by the run and by Check for bills, sales invoices in the accrual copy, `Decision` on each line (accept/adjust/reject, reason, who, when; only accepted and adjusted lines are built and posted; a re-run keeps decided lines), and `/accrue` (`console/accrue.py`). TECC September in `/accrue`: six bill-based lines matching TECC's own, two estimates, two to review, 22 bills with PDFs to read.

**Phase B: the schedule and /reconcile 2000.** Reworked on 30 Oct 2026 into a roll-forward reconciliation built on the
accountant's own structure of the balance, generic for every balance sheet account, linking each later transaction
to the earlier items it relates to: see "Phase B: the roll-forward working paper" below (draft, for review before
any code).

**Phase C: event costs (source 3).** Event calendar from the Event dimension's dates (and events on sales invoices);
expected cost types per event from recovery lines, else the history of similar events; covered by a bill tagged to the
event (in the month, or after month end and confirmed by its PDF), else a proposed missing accrual (recovery amount, or
the usual cost for that kind of event) with the usual supplier and lag. Needs: recovery↔cost pairs confirmed, the
calendar complete, an event type on the Event dimension if type-based patterns are wanted.

**Phase D: learning.** Rejections and adjustments with reasons become rules; each closed accrual against its bill sets
the cost's accuracy, and accuracy decides how much review it needs.

### Phase B: the roll-forward working paper, for any balance sheet account (draft, 1 Nov 2026, for review)

Built in `/reconcile` for every balance sheet account; accruals (TECC 2000) are the first account kind and the worked
example. Drafted 30 Oct 2026; made generic and given item matching on 1 Nov 2026.

**Why the current link is wrong.** `/reconcile 2000` today takes this month's posted accrual lines as the support.
That only agrees with Xero if every earlier accrual was fully reversed or cleared. TECC at 30 Sep 2026: closing
balance 47,943.28, September accrued 31,210.80, so about 16,732 is something else: older accruals still open,
reversals never posted, or other entries. Accruing (`/accrue`) is an action; it doesn't mean the balance agrees with
what was accrued. The brought-forward balance and what happened to it decide that. The same is true of every balance
sheet account: a balance is an opening balance plus what moved, and only the accountant can say what it should be
made of.

**Principles.**

1. **One mechanism for every account.** Opening + movements = closing; closing = the items still open, each in a
   component; the total must equal Xero's balance. Accruals, prepayments, provisions, deposits, loans, GST,
   intercompany and clearing accounts all use the same working paper, setup questions and sign-off.
2. **The accountant decides how the balance is analysed.** What the balance is *made of* is the business's
   accountant's call: utilities are always a month late, the audit fee is a standing 1/12 of the quote, audio-visual
   bills come late, the rest is ad hoc and shows up as invoices later. iLA asks for that structure up front, suggests
   it from history so nobody starts from a blank page, and never imposes its own.
3. **iLA links what happens later to what was there before.** Each transaction in the period that relates to an
   earlier item (its reversal, its bill, its amortisation, its repayment) is linked to that item, so every item has a
   story: raised, then reversed, billed, settled, with any difference shown.

#### 1. Account kinds: what makes the working paper generic

An admin presets each balance sheet account's **kind** in the chart of accounts, next to its reconciliation method
and threshold (the method becomes "roll-forward" for these accounts). The kind only supplies defaults: the column
names, the behaviours offered for components, how movements are recognised, and what counts as settling an item.
Everything else (the working paper, the setup, matching, sign-off) is the same code.

| Kind | Raised (+) | Released (−) | Settled by | Typical behaviours |
| --- | --- | --- | --- | --- |
| Accruals (2000) | Accrued | Reversed | The supplier's bill (usually coded to the expense account) | In arrears, standing, event-driven, ad hoc |
| Prepayments (1400) | Paid in advance | Amortised | The amortisation over the service period | Spread over a period, ad hoc |
| Provisions (leave) | Provided | Used / released | Payroll payments | Computed from a report, standing |
| Deposits / income in advance | Received | Earned / refunded | The sales invoice when the event is held | Event-driven, ad hoc |
| Loans | Drawn | Repaid | Repayments; interest | Agreed to a statement |
| GST | Collected / paid | Lodged | The BAS payment or refund | Computed from a report |
| Intercompany | Charged | Paid | The other entity's matching entry | Agreed to a statement |
| Clearing / suspense | In | Out | The matching out-entry | Should be nil |

The table is the starting set; a kind is data, not code, so a new kind (e.g. bonds, gift vouchers) is added in
admin by choosing its labels and behaviours. Balances are shown in the account's natural sign (a credit-balance
account like 2000 shows its balance as positive).

#### 2. The account's structure: components

Each account has a short list of components, the headings the accountant uses to explain the balance, plus two
fixed lines.

| Field | Example (2000) | Example (1400) |
| --- | --- | --- |
| Name | Utilities | Insurance |
| Behaviour | In arrears | Spread over a period |
| How to recognise it | Suppliers AGL, CS Energy; words "electricity", "gas"; expense accounts 6200-6220 | Supplier the broker; words "policy", "premium" |
| What it should be | About one month of the usual bill | The unexpired months of each policy |

Behaviours (each says what the component's closing balance should be; the kind decides which are offered):

| Behaviour | Example | Expected closing balance |
| --- | --- | --- |
| In arrears | Utilities, audio-visual | The usual cost for the months not yet billed (the usual lag, normally one month) |
| Standing (builds up) | Audit fee | Annual amount ÷ 12 × months since the last bill, less what was billed |
| Spread over a period (runs down) | Insurance, software | Each item's amount × unexpired days ÷ total days |
| Event-driven | AV for events, deposits | The items for events held (costs) or not yet held (deposits), not yet settled |
| Computed from a report | GST, leave provision | The figure from the report (BAS, leave balance), typed or uploaded |
| Agreed to a statement | Loans, intercompany | The other party's statement balance, typed or uploaded |
| Should be nil | Clearing accounts | Nil |
| Ad hoc | Anything else | No formula: each item must be backed by a later document, or carry a note |

Fixed lines:

- **Not assigned yet**: movements no rule or decision has placed. Must be nil before the month can be prepared.
- **Opening not analysed**: the part of the opening balance at the start date that nobody could break down (below).
  It isn't frozen: movements linked to it (section 5) reduce it, so it should run down to nil.

The structure is set up in the console (`structure` in the working paper) and editable in admin. A change applies
from the month it is made; signed months keep the structure they were signed with.

#### 3. First-time setup: iLA asks, the accountant answers

The first time an account is opened with no start date, the working paper asks three things in turn (the same three
for every kind; the wording comes from the kind):

```
/reconcile 2000 --Sep 2026

2000 Accruals hasn't been reconciled in iLA before.

1. Where should iLA start?  e.g.  start 1 Jul 2026
   → Opening balance at 30 Jun 2026 (Xero): $xx,xxx.xx

2. What is this balance usually made up of?
   From your journals it looks like (edit, add or remove):
     1  Utilities       in arrears            AGL, CS Energy
     2  Audit fee       standing              <audit firm>
     3  Audio visual    in arrears            <AV supplier>
     4  Ad hoc          backed by a later invoice
   Type changes in plain English, e.g. "audit fee is 1/12 of $24,000 a year",
   or << structure.csv · ok when done

3. Please give the breakdown of the opening $xx,xxx.xx at 30 Jun 2026, if you can.
   << opening.csv (component, description, amount, supplier or invoice if known)
   or type lines: utilities 3,200 June electricity
   or "don't know": iLA proposes a split from the journals before 30 Jun,
   and anything left over stays as "Opening not analysed"
```

1. **Start date.** One iLA start date per organisation (e.g. the start of the financial year), which an account can
   override. The opening balance is Xero's balance at the day before (from the trial balance report). Accepting it
   records who, when and the figure.
2. **Structure suggested from history.** iLA reads the copied transactions in the account (about 12 months before
   the start date, copied in the background like the accrual scan) and groups them by supplier, narration and the
   other side's account, the way the accrual engine already groups accruals, guessing each group's behaviour from its
   pattern (monthly and a month behind: in arrears; even monthly amounts: standing or spread; irregular: ad hoc). The
   accountant confirms, edits, merges or removes; plain English goes through the LLM, which drafts the change and
   shows it for OK before saving; nothing is saved without OK.
3. **Opening breakdown, if they can.** A full, partial or no breakdown are all fine. Where the accountant doesn't
   know, iLA proposes one from the entries still open at the opening date (for accruals, the open-accruals analysis
   in `accounting/balance.py`) for them to accept or edit. Whatever is left unexplained is the "Opening not analysed"
   line, shown and run down every month. The accepted breakdown lines become the opening **items**, each in a
   component, and are matched like any other item from then on (section 5).

#### 4. Each month: movements placed in components

For every month from the start date to the month asked for, iLA reads the movements in the account and sorts them
by the kind's movement types (for accruals):

| Movement | How it's recognised |
| --- | --- |
| Accrued (raised) | Manual journals crediting the account that aren't reversals |
| Reversed (released) | Debits in the account: manual journals, auto-reversals |
| Billed (settled in the account) | Bills coded straight to the account |
| Other | Spend money coded to the account, other manual entries |
| Not in iLA's copy | Xero's movement (trial balance) less what iLA read; should be nil, else something was edited after the copy or comes from a source iLA doesn't read |

Each movement is then placed in a component, in this order: the accountant's decision on that movement → the item it
is linked to (section 5) → the component's recognition rules → iLA's suggestion (shown as a suggestion) → Not
assigned yet. `assign 5 utilities` places one; a placement becomes a rule only when the accountant says so
(`assign 5 utilities always`).

#### 5. Linking this period's transactions to earlier items

Every open item, whether an opening breakdown line or an item raised in an earlier month, is looked for in what
happened since. iLA proposes links; the accountant confirms the ones it isn't sure of.

**What can be linked to an item.**

- **Movements in the account**: the reversal of an accrual, the amortisation of a prepayment, a loan repayment, a
  bill coded straight to the account.
- **Related transactions outside the account**: the evidence that settles the item even though it never touches the
  account. For an accrual this is the important one: the accrual is reversed on 1 October, and the supplier's bill
  is posted to the expense account (not to 2000) on 12 October. The bill settles the item; the reversal releases it.
  For a deposit it is the sales invoice when the event is held; for intercompany, the other entity's entry.

**How a link is found**, strongest first:

| Evidence | Example | Result |
| --- | --- | --- |
| The same reference | Accrual narration names invoice 3869; bill 3869 arrives | Linked |
| A reversal of it | "Reversal: Sep electricity accrual", same amount | Linked |
| Supplier + amount within tolerance + after the item's date | AGL accrual 3,200; AGL bill 3,350 for September usage | Proposed |
| Service period or event from the bill's PDF | PDF says "Event 25 Sep 2026", accrual for that event | Proposed |
| Same component and a similar amount | Ad hoc accrual 1,000; one ad hoc bill of 980 in the window | Suggested, needs OK |
| Narration only | "Plumbing works" vs a bill described "repair of pipes" | The LLM suggests, with its reasons, needs OK |

Links can be one-to-one, one item to several transactions (a bill arriving in two parts), or several items to one
transaction (one reversal journal clearing several accruals; one bill covering two months' accruals). Amounts are
always computed by code; the LLM only proposes which item a transaction belongs to and says why.

**What the links say about each item** (for accruals; each kind has its own version):

| | Bill in | No bill yet |
| --- | --- | --- |
| **Reversed** | Closed. Difference = bill − accrual (under- or over-accrued), shown on the item | Reversed too early: the cost is still owed but no longer in the balance (propose re-accruing) |
| **Not reversed** | Counted twice: the accrual should be reversed (reconciling item with an action) | Still owed: stays open, ages, flagged after the stale limit |

The difference on a closed item is the accrual's accuracy: it feeds Phase D (learning) and `/accrue`'s estimates,
and a component with repeated large differences is pointed out.

**In the console.** `items 3` shows each item's links: `3,200 Sep electricity accrual · reversed 1 Oct · AGL bill
INV-5512 12 Oct 3,350 · under-accrued 150`. `link 7 3` links movement or transaction 7 to item 3; `unlink 7`;
`why 7` gives the evidence for a proposed link; `links` lists the proposals waiting for OK. A confirmed link is kept;
a re-run never undoes it.

#### 6. The working paper

```
2000 Accruals · Sep 2026            Opening  Accrued  Reversed  Billed   Closing  Expected  Check
Utilities                             3,200    3,400   (3,200)      –      3,400    ~3,300   ✓
Audit fee                             6,000    2,000        –       –      8,000     8,000   ✓
Audio visual                          9,100   12,500   (9,100)      –     12,500   ~11,800   ✓
Ad hoc                                    …        …         …      …          …         –   ⚠ 1 billed, not reversed
Not assigned yet                          –        –         –      –      x,xxx             assign
Opening not analysed                  x,xxx        –         –    (xxx)    x,xxx
Total                                                                  47,943.28
Xero balance at 30 Sep 2026                                            47,943.28             ✓ ties
Links: 14 linked · 3 waiting for OK · 2 items still owed past 3 months
Type: assign 5 utilities · items 3 · links · why 2 · structure · opening · months · prepare
```

- **Rows** are the components; **columns** are the roll-forward, named by the account's kind (a prepayment account
  shows Paid in advance / Amortised). The total of the closing column is the supporting balance; it must equal
  Xero's balance.
- **Reconciling items** come from the links: an item billed but never reversed, a reversal with no item, an item
  reversed too early. Each has an action, e.g. "reverse 4,200: bill 3869 came in on 12 Aug".
- **Expected and Check** compare each component with what its behaviour says it should be, so the table says which
  kind of cost is off, not just that the total is off.
- `items 3` lists a component's open items with their links (section 5); `why 2` explains its expected figure;
  `opening` shows the opening breakdown and how much of it is still open; `months` shows the roll-forward month by
  month from the start date.
- **Signing** follows `/reconcile`'s rules (prepared once the difference and Not assigned yet are nil, then
  reviewed). A signed month's closing items become next month's opening items, so later months just carry on. If
  Xero's balance for a signed month changes (something posted with an earlier date), iLA says by how much and clears
  the sign-off.
- **The pack** (`/reconcile --Sep 2026`) lists roll-forward accounts with the other methods: balance, support,
  difference, items waiting for a link, sign-off.

#### 7. Links to the rest of iLA

- **`/accrue` ↔ `/reconcile`.** `/accrue` is the action (what to accrue this month); `/reconcile 2000` is the check
  (does the balance make sense). `/accrue` ends with "Then: /reconcile 2000 --Sep 2026". Last month's open items and
  their link status become `/accrue`'s brought-forward source (source 1): "4 still open, 2 have bills in: reverse
  them?"
- **The structure feeds `/accrue`.** A standing accrual ("audit 1/12 of $24,000") is proposed every month; an
  in-arrears component's usual lag and amount inform the recurring estimate. Defined once by the accountant, used by
  both the action and the check. (Timing: open question.)
- **Reasonableness (source 5)** stays in `/accrue`: it asks whether this month's accrual is enough, not whether the
  balance ties.
- **The existing methods stay.** Bank, subledger and nil keep their own working papers; "schedule" (an uploaded
  CSV) remains for accounts nobody wants to roll forward. `/reconcile` steps 3 (GST, fixed assets) and 4
  (intercompany) become roll-forward kinds rather than separate builds.

#### 8. Data (first cut)

- Organisation: `recon_start` (date). Account: `recon_start` (override), `recon_kind`, and "roll-forward" as a
  method value.
- `AccountKind`: name, column labels, behaviours offered, movement rules, what settles an item (admin-editable data;
  the starting set from section 1 loaded by a migration).
- `ReconComponent`: account, name, behaviour, recognition rules (suppliers, words, other-side accounts),
  expected-amount settings (annual amount, lag months, service period), order, valid from / to.
- `ReconItem`: account, component, origin (opening breakdown, journal line, bill line, manual), date, description,
  supplier, reference, amount, open amount, status (open, released, settled, closed), difference.
- `ItemLink`: item, the linked transaction (type, id, line), role (release, settlement, part settlement), amount,
  how found (reference, reversal, supplier+amount, PDF, component, LLM), confidence, confirmed by / when.
- `MovementPlacement`: the accountant's decision on one movement (component, who, when, "always" → a rule).
- `Reconciliation` keeps its sign-off; it gains the roll-forward figures and the structure version it was signed with.
- Balances from the trial balance report at each month end; movements and related transactions from iLA's copy
  (manual journals, bills, sales invoices, bank transactions), re-copied for the months asked for plus the close
  window after, in a background job.

#### 9. Decisions and open questions

Decided (30 Oct and 1 Nov 2026): the mechanism is a roll-forward (opening + movements = closing = open items); it is
generic for every balance sheet account, with the account's kind supplying labels, behaviours and matching rules;
the accountant defines the components; iLA suggests the first structure from history; iLA asks for the opening
breakdown at the day before the start date, "if you can", and keeps whatever is unexplained visible; the support for
2000 is all open items, not just the month's lines; iLA links each transaction in the period, in or outside the
account, to the earlier item(s) it relates to, proposing what it isn't sure of for the accountant's OK.

Open (to settle at review):

1. The opening breakdown: CSV upload and typed lines enough, or also paste straight from an Excel workpaper?
2. Placements and links become rules only when the accountant says so (proposed), or automatically?
3. `/accrue` using the structure (standing accruals proposed, lags): in Phase B, or after it?
4. Stale limit: flag open items older than how many months (3 proposed)?
5. How far back to read to suggest the structure and explain the opening (12 months proposed)?
6. Amount tolerance for proposing a supplier + amount link (proposed: within 10% or $100, whichever is larger), and
   the difference above which a closed item is flagged as under- or over-accrued.
7. Which kinds after accruals, and in what order (proposed: prepayments, then GST, then intercompany)?
8. This section is now about `/reconcile` as a whole: move it to its own design doc (`docs/Reconcile — Design.md`)
   and keep a pointer here?

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
