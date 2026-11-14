# Labour — Design

Draft for approval, 14 November 2026. Step 2 of the month-end review cycle (`docs/Month-end Review — Design.md`,
section 9). Worked through with TECC's October 2026 pay runs.

## 1. The idea

One **labour ledger** per organisation, built from the payroll system's pay-run files. Everything else reads from it,
in any order:

| Purpose | What it does with the labour ledger |
|---|---|
| a) Review | Labour for the month per the pay runs vs what is in Xero, by department, event, employee and day. |
| b) Accrue | Days of the month not yet in Xero: the pay-run journal for those days, or an estimate if no file yet. |
| c) Tag | Each employee's cost by day goes to the events running that day; the rest is Unassigned. |

The output of a pay run is a **pay-run journal**: one Xero import file per pay run, split by month, with
Departments and Event tracking. While the accountant still posts the payroll journal by hand, iLA's journal is an
**Expected** journal for review: iLA compares it with what was posted. Later it replaces the manual journal: the
accountant approves it and imports the file (iLA never posts to Xero).

## 2. What TECC's files give us

The Ville sends a Micropay **employee breakdown** (xlsx) for each fortnightly pay run. Sheet *Micropay Breakdown*,
one line per employee × cost account × pay component.

| Column | Use |
|---|---|
| Employee Code, Full Name | Who (the only personal fields kept) |
| Pay Period ID, Period End Date, Pay Advice Print Date | The pay run, its last day, the pay date |
| Cost Account Code / Description (700-3000 F&B Service …) | The cost centre: decides department and whether it can go to an event |
| Transaction Type, Pay Comp/Add Ded Code and Desc | What it is: ordinary hours, loadings, super, sick leave, allowances, termination |
| Leave Start Date, Leave End Date | **The days worked**, on every line (loadings are single days; ordinary hours and super often span days) |
| Hours, Amount | Hours and $ |

Everything else (date of birth, age, hire date, super fund, pay class, …) is **dropped on upload** and never stored.

The file excludes payroll tax and workcover. It ties to Ville's FNE journal in Xero to the cent:

| Pay run | File | FNE journal in Xero |
|---|---:|---|
| 04/10/2026 | 17,948.18 | 6345 15,641.85 + 6355 146.74 + 6370 2,159.59 |
| 18/10/2026 | 46,435.76 | 6345 39,000.19 + 6355 371.11 + 6370 4,755.66 + 6312 9.95 + 6644 2,298.85 |
| 01/11/2026 | 74,603.06 | not in Xero yet (TECC's own draft: `output_tecc.csv`) |

On-costs on the run total: payroll tax **4.95%** (6335), workcover **1.711%** (6380), the same in every run.

## 3. Settings (Admin → Labour → Settings, per organisation)

**Source.** Micropay employee breakdown (first format). Later: Xero Payroll, other CSV layouts.

**Cost accounts.** One row per payroll cost account:

| Cost account | Department (Xero Departments) | Can go to an event | GL |
|---|---|---|---|
| 700-3000 TECC - F&B Service | Food and Beverage Services | Yes | 6345 |
| 700-5000 zTECC - Culinary and Stewarding | Food and Beverage Services | Yes | 6345 |
| 700-2600 TECC - Cleaner | Overhead & Venue Costs | Yes | 6345 (not 6644, for now) |
| 700-2100 TECC - Fixed Wages | Overhead & Venue Costs | No (Unassigned) | 6345 |
| 700-2000 TECC - Event Sales | Event Hire & Service Recoveries | No (Unassigned) | 6345 |

A new cost account in a file is flagged on upload; it must be mapped before the journal can be built.

**Pay components → GL.** By transaction type or pay component, the first match wins:

| Component | GL |
|---|---|
| Super | 6370 Superannuation |
| Sick Leave | 6355 Sick Leave |
| Allowances (e.g. Nauseous allowance, Tool Allowance) | 6312 Allowances |
| Everything else (ordinary hours, loadings, overtime, termination pay, auto pay) | the cost account's GL (6345) |

**On-costs.** Payroll tax 4.95% → 6335, workcover 1.711% → 6380, on each line's amount. iLA learns the rates from
posted payroll journals and shows them for confirmation; a person can override them.

**Journal.** Credit account 2350 Payroll Clearing; narration `Ville for TECC Payroll Journal FNE: 1/11/2026`;
tax rate BAS Excluded; tracking names *Departments* and *Event*.

**Events.** Kept on the Event dimension (built in step 1): bump-in days (default 2) and bump-out days (default 2),
overridable per event; an event longer than 7 days is a *season* unless set otherwise.

## 4. Event dates

Today only 37 of TECC's 221 events have dates, and they are hidden in a JSON field on the Event admin page (the
page that fails with Bad Request 400). This step fixes both:

- **A Dimension values admin page** with search, a filter by dimension, and real columns: Code, Name, Start, End,
  Bump-in, Bump-out (blank = the default), Season (automatic, can be overridden), Active. The Event page no longer
  lists every value as a form, so it saves.
- **Suggested dates**, confirmed by a person (`Suggest dates` on the admin list):
  1. the code: `261017TOW` → 17 Oct 2026 (91 of TECC's codes start with YYMMDD);
  2. sales invoices tagged to the event: invoice dates and dates in descriptions (`Arena - 09.10.26`).
- `/createTag` keeps working: `Start date` and `End date` columns in the CSV fill the same fields.

## 5. How iLA works out the event

Each pay line is first **spread over its days**:

- wages lines: evenly over Leave Start → Leave End;
- super, payroll tax and workcover: **follow the employee's wages** in that pay run, day by day and line by line,
  so they land on the same department and events as the wages they relate to.

Then each **employee-day** in a cost account that can go to an event is allocated, the first rule that applies
winning:

| # | Rule | Reason shown |
|---|---|---|
| 1 | A person set this employee's day (or a range of days) | Changed by you |
| 2 | A person set the whole day to an event | Day set by you |
| 3 | Event day of one or more events (start → end) | Event day; split equally if several |
| 4 | Bump day of one or more events (± bump days) | Bump-in −1 / bump-out +2; split equally |
| 5 | A season is running (e.g. 2627FIRE, 1 Oct – 28 Feb) | Season; split equally if several |
| 6 | Nothing | No event → Unassigned |

Cost accounts that can't go to an event (Fixed Wages, Event Sales) are always Unassigned, with reason *Not
taggable*. Changes made by a person are kept and re-applied when the run is uploaded again or the event dates change.

## 6. The pay-run journal

One Xero manual-journal import file per pay run, in TECC's layout (`*Narration, *Date, Description, *AccountCode,
*TaxRate, *Amount, TrackingName1, TrackingOption1, TrackingName2, TrackingOption2`).

- **One journal per month covered.** FNE 1/11/2026 covers 19 Oct – 1 Nov: the days in October post on
  **31/10/2026**, the day in November on **1/11/2026**, both in the same file.
- **Lines by account × department × event.** Xero holds one Event per line, so a line split across three events is
  three lines; Unassigned lines have no Event. Each journal balances against 2350.
- **Rounding.** Lines are rounded to the cent; any cent left over goes to the largest line, so the file ties to the
  pay-run total exactly.
- **Checks before download:** file total = pay-run total + on-costs; every cost account mapped; every Event code
  exists in Xero's Event tracking (a new event must be added in Xero first).

**The journal as an overlay journal.** Each month's journal is an `ADJ-nnnn` record (the basic overlay-journal record
this step creates): lines with tracking, made by *labour*, evidence = the pay-run file, bucket **Expected**.

| What happens next | Result |
|---|---|
| The accountant posts their own payroll journal (today) | Refresh finds it (FNE date in the narration); iLA's journal is *superseded* and iLA shows the difference by account and department. Labour is tagged to events with `/splittag --labour` (section 8c). |
| A person approves iLA's journal (later) | Expected → To post; the stamped import file is built; the accountant imports it; refresh finds it in Xero and it leaves the buckets. The Event tracking comes in with it. |

## 7. Admin → Labour

**Upload.** Choose the pay-run file (Admin → Labour → Upload, or `/labour << file.xlsx` in the console). iLA checks
it ties to its own Summary total, shows what it read (run, period, days, employees, total, new cost accounts or
components) and saves it. Uploading the same pay run again replaces it; changes made by a person are kept.

**Labour view.** Choose the month; it shows every pay run touching that month, only the days in that month.

1. **By employee** (the default): one row per employee; columns Department, then each event, Unassigned, Total.
   Click a name to open their days: date, hours, $, cost account, components, event(s) and reason. Change the
   event for a day or a range of days (or set it to Unassigned), with a note.
2. **By day:** one row per day; columns taggable $, Unassigned $, the events found, event day / bump day markers.
   This is the quickest place to fix the calendar: set a day to an event and everyone's taggable cost that day
   moves. (October: about $58k of taggable labour fell on days with no event, e.g. 18, 28, 29 and 31 Oct.)
3. **By event:** one row per event with its dates, employees, hours and $ (wages, super, on-costs).
4. **Journal:** each month's journal lines, the ties, and *Download import file*.

Totals tie across all four views. Employee name and code only; nothing else personal is shown or stored.

## 8. The three purposes

**a) Review: `/labour --Oct 2026`** (console, figures only, L0 then drill down)

| Line | |
|---|---|
| Labour per pay runs (Oct days) | 126,711 |
| In Xero now | 54,998 |
| Difference | 71,712 |
| of which: pay run 01/11 not in Xero (19–31 Oct) | 64,353 |
| of which: September accrual reversed vs September's share of FNE 4/10 | 7,359 |

Then by department, by event, by pay run, and `L1 <department>` for employees. It ties to the `lab` line in `/review`.

**b) Accrue.** With the pay-run file uploaded, the October part of the pay-run journal *is* the accrual, at actual
cost. With no file by close, iLA estimates the stub days from the last two pay runs (daily rate × days, adjusted for
events) into Expected; when the file arrives, its journal supersedes the estimate.

**c) Tag: review first, then `/splittag`.** The labour journal can differ from what is in Xero (accruals, reversals,
manual adjustments, timing). So it isn't used to tag Xero's lines one by one; it gives the **event shares** for the
month, and `/splittag` pro-rates the actual labour in Xero (6300-6380) by them.

1. **Review the variance.** `/splittag --labour --Oct 2026` first shows the labour journal for the month against
   the actual labour in Xero on the same accounts:

   | | $ |
   |---|---:|
   | Labour journal, October days (6300-6380) | 126,711 |
   | Actual labour in Xero, October (6300-6380) | … |
   | Variance | … (…%) |

   Then each event's share of the labour journal (e.g. `261017TOW …%`), and Unassigned's share.
2. **Split.** `approve` tags every labour line in Xero for October (all types: journals, bills, bank) pro-rated
   by those shares. The Unassigned share stays untagged, so it shows as Unassigned in `/pl --event`. It's an ordinary
   `/splittag` run (Rule `labour`, saved): running it again for the month replaces the earlier split, and it shows
   under Admin → Console → Split runs and Core → Assignments.
3. **When the variance is large,** fix the cause first (a missing pay run, an accrual, the event calendar) and run it
   again. iLA shows the variance and leaves the decision to you; there is no automatic threshold.

- **Accounts:** the `lab` group from Review settings (TECC: 6300-6380), changeable with `--gl` as in any
  `/splittag`.
- **Shares** come from the labour view after your changes, wages plus super and on-costs, for the days in the
  month.
- A journal imported from iLA already carries Event tracking. `/splittag --labour` still works then, and its split
  wins over Xero's tracking as any split does.

## 9. Data (first cut)

| Record | Holds |
|---|---|
| LabourSettings | source, credit account, narration pattern, tracking names, on-cost rates, bump days, season length |
| CostAccountMap | cost account code and name → department, taggable, GL |
| ComponentMap | transaction type / pay component → GL |
| Employee | code, name |
| PayRun | pay period ID, period start and end, pay date, file name and hash, totals, uploaded by and when |
| PayLine | pay run, employee, cost account, component, transaction type, dates, hours, amount |
| LabourDay | pay line × day: amount, hours (derived) |
| LabourAllocation | employee × day × event: share, rule, reason (derived) |
| LabourOverride | employee or whole day, date range, event or Unassigned, note, who and when |
| Adjustment (ADJ) | overlay journal: number, period, date, lines (account, department, event, amount), made by, evidence, bucket, status, superseded by |
| Event fields | start, end, bump-in, bump-out, season on the dimension value |

## 10. Build steps

1. **Event dates.** Dimension values admin page (fixes the 400), date and bump fields, suggested dates, `/createTag`
   date columns. Also: approving `/createTag` re-applies Xero tracking to lines already copied.
2. **Upload and the labour ledger.** Settings, maps, Micropay reader (personal fields dropped), pay runs and lines,
   ties.
3. **Allocation and Admin → Labour view.** Day spread, rules, overrides, the four views.
4. **Overlay journal and the pay-run journal.** ADJ record, journal by month, import file, superseded by the posted
   payroll journal with the difference shown.
5. **Tags and review.** `/splittag --labour` (variance first, then the pro-rated split), `/labour` review, stub
   estimate when there's no file.

Tests use a made-up pay-run file in Micropay's layout; TECC's real files stay out of git.

## 11. Decisions (14 November 2026)

| Topic | Decision |
|---|---|
| Multi-day lines | Spread evenly over their days (super and on-costs follow the wages). |
| Taggable | By cost account code; Fixed Wages and Event Sales are Unassigned. |
| Several events on a day | Split equally; event days beat bump days, bump days beat seasons. |
| Bump days | ± 2 by default, editable per organisation and per event. |
| Cleaner (700-2600) | 63xx accounts, department Overhead & Venue Costs, for now. |
| Department | Xero *Departments*: Food and Beverage Services, Overhead & Venue Costs, Event Hire & Service Recoveries. |
| On-costs | Tagged the same way as the wages they relate to (department and event). |
| Who posts | The accountant, today; iLA's journal is Expected, for review. Later iLA's file replaces it, approved and imported by the accountant. |
| Personal data | Employee code and name only. |
| Journal by month | A pay run across two months gives two journals in one file (last day of month, and the pay-run end date). |
| Tagging labour | `/splittag --labour`: review the variance between the labour journal and Xero's labour first, then pro-rate all of the month's labour in Xero (6300-6380) by the journal's event shares; Unassigned stays untagged. |
