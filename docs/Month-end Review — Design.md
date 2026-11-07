# Month-end Review — Design

Agreed with the user on 7 November 2026 (design version 4). It covers the month-end review cycle: the landing view, overlay journals, `/review`, `/status` as the cockpit, labour, and the lock.

## Where this starts from

- **Labour today.** TECC's payroll is run by The Ville and arrives in Xero as fortnightly manual journals, e.g. *"Ville for TECC Payroll Journal FNE: 12/07/2026"*. They post across 6345 event staff, 6300 annual leave, 6335 payroll tax, 6370 super, 6380 Workcover and 6344 bonus. Month-end labour accruals are typed by hand into 2000.
- **The accrual engine** (`/accrue`) keeps payroll in its own section but doesn't accrue it yet.
- **`/status` has gaps.** With no period it shows cash at bank and a count of Xero items not yet reconciled. With a period it shows only budget and last-year measures, with no cash or bank at all. Neither shows the bank reconciliation status from `/reconcile`.

## 1. The idea

**iLA's view of a month = Xero's actuals + a layer of overlay journals.** The senior accountant works in that view, never in Xero, and keeps refining the layer until the month is locked.

    Xero now  +  To post  +  Expected  =  Landing (where it should land)

- **To post:** agreed or started by a person, not yet in Xero.
- **Expected:** iLA's estimates, not yet approved.
- **The landing shows both buckets as separate columns.**
- **iLA never posts to Xero.** Approved overlay journals go into a stamped import file that a person imports.

## 2. Overlay journals and the two buckets

Every action produces the same thing: an **overlay journal**. It can be an accrual, a reclass, a correction, a provision or an estimate.

| Field | Example |
|---|---|
| Number | `ADJ-0142` (never changes) |
| Period | Oct 2026 |
| Lines | Dr 6345 9,840 · Dr 6370 1,181 · Dr 6335 482 · Cr 2000 11,503 (with tracking/event where known) |
| Made by | iLA (which action: `/accrue` labour, `/reconcile 2000`, `/reclass` …) or a person |
| Why, and the evidence | "6 stub days (26–31 Oct) not covered by FNE 25/10; FNE 08/11 covers them: 6/14 of it" |
| Confidence | Evidenced / estimated / judgement |
| Reverses | On the 1st of next month (default for accruals), or not |
| Bucket | Expected or To post, with who moved it and when |

| Step | What happens |
|---|---|
| Into Expected | An iLA estimate from an action. It counts in the landing straight away, in the Expected column. |
| Into To post directly | Anything a person makes or starts (`/adj`, a reclass they set up, an accrual they type). A person can put one of theirs into Expected instead, as an estimate only (`expect`). |
| Expected → To post | One person approves it, in `/status`. |
| To post → posted | The import file is built and stamped, and once imported the journal is marked posted. It stays counted in the layer until the next refresh **finds it in Xero**, by the ADJ number in the narration or by its lines. Then it leaves the buckets. This is what stops double counting. |
| Rejected or withdrawn | It leaves the buckets. The reason is kept and can become a rule. |
| Superseded | The real thing (a bill, or the user's own Xero journal) arrived before the overlay journal was posted. The reconciliation linker connects them, takes the overlay journal out of the buckets and shows how far the estimate was out. |

Overlay journals that reverse do so in the layer the next month, as Xero's auto-reversing journals do.

## 3. The views

`--landing` on `/pl`, `/bs`, `/tb` and `/pl --event` shows these columns: Xero now · To post · Expected · Landing · Budget · Last year · Landing vs budget.
- `why <account>` lists the Xero lines and the overlay journals behind a figure.
- The balance sheet uses the same layer, so `/reconcile` sees the landing balance too.

## 4. The cycle

The cycle repeats until the month is locked:

1. **Refresh** (automatic, or `reread`): copy Xero. Overlay journals now found in Xero leave the buckets.
2. **Review** (`/review`): the core P&L checks plus the review requests.
3. **Act** (`/accrue`, `/tag`, `/reclass`, `/reconcile`, `/adj`): put overlay journals into the buckets.
4. **View & approve** (`/status`, `/pl --landing`): approve Expected → To post, then build, import and stamp. Once the posted journals are in Xero, the next refresh starts the next cycle.

**Lock** needs three things:
- every `/review` finding reviewed
- every account reconciled and signed
- To post empty (or deliberately carried forward)

The lock takes a snapshot: Xero's TB, the layer, the decisions, and who locked it and when. Anything still in **Expected is archived**: it can be viewed, but has no bearing anywhere in the console. If Xero later changes for a locked month, iLA flags it with the amount.

## 5. `/review`: core P&L checks + review requests

For now `/review` covers the **P&L**. The balance sheet comes later on the same design. `/review` = the core checks + every confirmed review request, as one numbered list of findings. With no period it looks at the **last full month**.

### Part A: core P&L checks (on by default)

| # | Check | Compared with | Shown as |
|---|---|---|---|
| C1 | Month: every P&L line and subtotal | Last month | $ and % |
| C2 | Month: every P&L line and subtotal | Same month last year | $ and % |
| C3 | Month: every P&L line and subtotal | Budget | $ and % |
| C4 | Year to date: every line and subtotal | YTD budget | $ and % |
| C5 | COGS % of revenue | Last month, last year, budget | points |
| C6 | Labour % of revenue | Last month, last year, budget | points |
| C7 | EBITDA % of revenue | Last month, last year, budget | points |
| C8 | Net profit % of revenue | Last month, last year, budget | points |

- **Setting up each check (admin):**
  - Each check has an on/off switch and a materiality the user sets.
  - **The default materiality is any movement over $1,000 either way.** Each check can instead use an amount, a %, points (ratios) or a combination ("over $1,000 and over 5%", "over $5,000 or over 10%").
  - Below materiality nothing is raised.
- **EBITDA** = net profit + interest + tax + depreciation and amortisation. Which accounts count as COGS, labour, interest, tax, depreciation and amortisation comes from Xero's account types and can be changed in admin. For TECC, labour = 6300–6380.
- **iLA explains each variance** from what it can see:
  - the bill or journal behind it
  - the overlay journals in the layer
  - events this month against last year
  - the user's notes on the same line in earlier months
- **Ad-hoc events are told to iLA once**, as a note. The variance then counts as explained, both this month and when this month is "last year" next year.

### Part B: review requests (admin)

Admin › Review › Review requests. Each starts as a blank canvas: a plain-English prompt, e.g. RR-01 *"iLA, please look at any revenue unassigned to any events."*

| Field | Content |
|---|---|
| Number | `RR-01` (never changes) |
| Request | The user's prompt |
| Trial-run months | Preset per organisation, default 2 |
| The check iLA worked out | In plain English, and the saved check (JSON) underneath, read-only |
| Trial result | The table from the last trial run, exactly as it will appear in `/review` |
| Status | Draft → Trial run → **Confirmed** · Off. **Only an admin confirms.** |
| History | Every version of the prompt and check, who confirmed it and when |

How a request becomes a check:
1. **Write** the prompt.
2. **Trial run.** The LLM turns the prompt into a saved check made of iLA's building blocks:
   - which lines: accounts, account types, contacts, dimensions, references
   - which months
   - what to compare
   - what condition makes a finding
   - which columns to show

   Code runs it on the last 1–2 months and shows the result on the admin page.
3. **Adjust** the prompt and trial run again if needed.
4. **Confirm & save** (admin). From then on, code reruns the saved check on every `/review`, with no LLM at run time. The output keeps the trial run's layout and is appended after the core checks.

Further rules:
- **If iLA can't do what's asked, it says so at the trial run** instead of guessing.
- **Editing a confirmed request** sends it back to Trial run. The confirmed version keeps running until the new one is confirmed.
- **A request can carry its own materiality and an action**, e.g. RR-01's findings open `/tag`.
- **Event recovery % is a review request for TECC**, not a built-in check.

### Working the review

    why 3                 the figures, what it's compared with and the evidence
    note 4 <text>         add what iLA can't know
    ok 1                  reviewed: no action needed
    act 6                 open the action that fixes it
    later 2               not yet: stays open

A finding is reviewed once it's OK'd, noted or acted on. Notes are kept, reused when the same thing comes up again, and become the commentary in the management pack. **The management pack is the landing view by default** (Xero now + To post + Expected). Admin can change that per organisation.

## 6. Actions

| Action | What it does | Lands in |
|---|---|---|
| `/accrue` | Month-end accruals: bills after month end, recurring estimates, payroll | Expected (iLA's) |
| `/tag` | Tags costs and revenue to events (iLA dimensions; Xero is not changed) | The event view |
| `/reclass` | Moves amounts between accounts, by rule or one-off (builds on the parked `/split` design) | Expected |
| `/reconcile` | Reconciling items that need a journal | Expected |
| `/adj` | Any journal, typed or drafted from plain English | To post, or Expected with `expect` |

## 7. `/status`: the cockpit

| Block | Content |
|---|---|
| Headline | Revenue, GP %, labour %, net profit: Xero now → landing, against budget and last year |
| **Cash and bank** | Each bank account at the date: Xero balance, statement balance (from `/reconcile`), difference, Xero items not yet reconciled (count, amount, oldest), sign-off. **With or without a period.** |
| Reconciliations | Accounts reconciled, prepared, reviewed and to do; unexplained differences |
| Review | Findings reviewed / open, and the biggest open ones |
| Overlay journals | Expected and To post: number, description, made by, amount, confidence, status |
| Since your last look | New Xero transactions, overlay journals found in Xero, landing movements |

    approve 3             Expected → To post (one person is enough)
    expect 4              To post → Expected (an estimate only)
    approve all evidenced
    adjust 5 9,000 <reason>
    reject 6 <reason>
    why 2
    post                  build the stamped import file of everything To post
    posted 3-7            stamped as posted

## 8. Making the review intelligent

For every check and line, iLA gives **an expected figure, with a reason, compared against the landing**. Code works out every amount. The LLM explains, drafts journals from plain English, and suggests where rules run out.

1. **Coverage, not averages.** Which days, events or bills of the month actuals already cover: pay runs, bill service periods, each event's bills.
2. **Drivers.** Event-related costs scale with the events held.
3. **Ratios that should hold:** the core ratios, plus any in review requests.
4. **Evidence beats estimates, automatically.** When the evidence arrives, the Expected figure is replaced or superseded.
5. **Learning.** Rejections and notes with reasons become rules. Each estimate's accuracy against what arrived sets how much attention its stream needs.
6. **Plain English.** iLA drafts overlay journals and waits for an OK.

## 9. Labour

The core C6 check flags labour, and `act` opens the payroll section of `/accrue` (also `/labour --<period>`).

**Input sources are set per organisation.** iLA is built for many businesses, and labour often has several sources. The labour lens uses the best one available for each day.

| Source | Gives |
|---|---|
| Payroll journals in Xero | Pay runs recognised by narration (TECC: Ville "FNE dd/mm/yyyy") |
| Xero Payroll pay runs | Period, employees, earnings, super, tax |
| Payroll or HR system export | Pay runs, employees, leave balances (TECC: possibly UKG) |
| Roster or timesheet export | Hours by day, person and event: the best evidence for stub days |
| Agency or labour-hire bills | Hours and rates, read from the PDF |

1. **Coverage by pay run.** Which days of the month are covered by pay runs in Xero, and which stub days aren't.
2. **The stub.** If the next pay run is in Xero, the stub is its share for those days (evidenced). Otherwise it's the daily rate of the last two pay runs × the stub days, adjusted for events (estimated). On-costs come from learned ratios, or the actual split when evidenced.
3. **Into the buckets.** If the user already posted a salary accrual in Xero, iLA compares the two figures and adds nothing. Otherwise it puts an ADJ (Dr labour, Cr 2000, reversing the 1st of next month) into Expected.
4. **Checks.**
   - labour % of revenue
   - event staff per event
   - wages recovered ÷ event staff
   - super ÷ salaries
   - leave accruals sitting in 2000 (reclass to 2310)
   - bonus pattern

## 10. How this fits what exists

| Already built | Becomes |
|---|---|
| Expectation Ledger accrual journals, `/accrue` decisions | The first action producing overlay journals into Expected |
| Open-item reconciliation reconciling items | Overlay journals into Expected |
| `/tag`, `/splittag`, `/pl --event` | Actions on the event view |
| Import file and stamping | How To post reaches Xero (`post`, `posted`) |
| The reversal linker | Finds posted overlay journals in Xero, and superseded ones |
| `/status`, `/reconcile --bank` | The cockpit, with the bank reconciliation status brought in |

## 11. Build order

1. **`/review` on the P&L.**
   - core checks C1–C8, each with on/off and materiality in admin
   - review requests: prompt → trial run → admin confirms, with the saved check and history
   - notes and review status
2. **Labour.** Input sources per organisation, pay-run coverage, stub accrual, on-cost ratios and driver checks. This step also creates the overlay journal as a simple record (number, lines, bucket).
3. **`/accrue` and `/reconcile` producing overlay journals.** Refresh finds posted ones in Xero.
4. **`/reclass` and `/adj`** (plain English), superseded detection, accuracy tracking, and the lock with its snapshot (Expected archived).
5. **The two buckets worked from the console.**
   - `--landing` on `/pl`, `/bs` and `/tb`
   - `/status` as the cockpit: cash and bank reconciliation status with or without a period; approve, expect, adjust, reject, post and posted

Until step 5, `/review` works on Xero now plus whatever is already in the layer. Steps 2–4 show their overlay journals in their own working papers and in admin.

## 12. Decisions (7 November 2026)

| Question | Decided |
|---|---|
| The landing | Includes both buckets as two columns. To post = agreed or started by a person; Expected = iLA's estimates until approved. |
| Core checks | Users set the materiality. The default is any movement over $1,000 either way. Each check can use an amount, %, points or a combination. EBITDA as above. No period means the last full month. |
| Labour data | Flexible input sources per organisation; many businesses have several. |
| Management pack | The landing view by default, changeable per organisation in admin. |
| Approval | One person approves Expected → To post. |
| Expected at lock | Archived: viewable, no bearing anywhere in the console. |
| Review requests | Only admins confirm them. The trial run is preset to 2 months. |
