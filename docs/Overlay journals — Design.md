# Overlay journals: one bucket, composed in the console — Design

Approved 21 November 2026. Build steps 1 to 4 are built (see the note at the end); how to use it is in
`docs/How To.md` (Overlay journals).

## 1. The idea

**iLA's view of a month = Xero + overlay journals (OJ).** An overlay journal is the difference between what the
accounts should be and what is in Xero. There is **one bucket** of them, whoever or whatever made them, with **two
statuses**:

| Status | Meaning |
|---|---|
| **Draft** | Being worked on: by you, or put there by iLA (labour, accruals) for you to take or change. |
| **To be posted** | Its Xero import file has been generated. Generating the file is what makes it To be posted. |

Once a journal is in Xero it **leaves the bucket**: it's now part of Xero's figures, so keeping it would count it
twice. Nothing else: no Expected, Posted, Superseded or Rejected statuses. iLA never posts to Xero.

You compose, change and review them in one place in the console (`/OJ`), seeing the P&L with them on top as you go.

## 2. From three places to one

| Today | Becomes |
|---|---|
| Core → Journals / Journal lines | Unchanged in substance: this is iLA's **copy of Xero's** manual journals, i.e. "Xero now", not journals outside Xero. Renamed **Xero manual journals (copy)** so it isn't confused with the bucket. |
| Core → Overlay journals (ADJ), statuses Expected / To post / Posted / Superseded / Rejected | **The one bucket.** Statuses cut to Draft and To be posted. |
| Accounting → Expectation Ledger journals | No longer a journal store. `/accrue` stays as the **working paper** (what's expected, the evidence, your accept/adjust/reject); its result is **one overlay journal** in the bucket. |
| Labour's pay-run journals and estimate | Already overlay journals; they become ordinary Drafts in the bucket. |

Rule from now on: **working papers explain a journal; the journal itself lives only in the bucket.** `/accrue`,
`/labour`, `/reconcile` and later `/reclass` all put their journals there, and you finish them all the same way.

## 3. What an overlay journal holds

| | |
|---|---|
| Number | `OJ-0012`, given when the journal is created, never changed and never used again. A suffix shows where it stands (below) |
| Status | Draft or To be posted |
| Date, month, narration | The narration carries the number, so iLA can find it in Xero later |
| Lines | Account, description, amount (debit / credit), Departments, Event, tax rate (BAS Excluded unless you say otherwise) |
| Reverses | No, unless you say so. `rev` = day 1 of the next month; `rev 15/11` = that date |
| Made by | You, or iLA with where it came from (labour pay run 1/11, accruals October) and its evidence |
| Seen by | See "Private drafts" below |
| History | Every change: who, when, before and after |

**iLA's drafts and yours.** A draft iLA made is kept up to date by iLA (e.g. when you change the event dates) until
you change it or mark it To be posted; from then on it's yours and iLA leaves it alone. If iLA's own figure later
moves, it tells you the difference instead of overwriting.

**The number and its suffix.**

| Shown as | Meaning |
|---|---|
| `OJ-0012-d` | Draft |
| `OJ-0012-p1` | To be posted: import file 1 generated |
| `OJ-0012-d` | Switched back to Draft; file 1 is cancelled |
| `OJ-0012-p2` | Generated again: file 2 |

- The suffix with its file number goes into the narration of the import file, so after a refresh iLA knows exactly
  which file was imported. If an older, cancelled file was imported by mistake, iLA says so instead of quietly
  accepting it.
- Once it's in Xero the journal leaves the bucket and 0012 is finished; the next new journal is 0013. A deleted
  draft's number isn't reused either (the audit log explains the gap).
- You only ever type the number: `open 12`, `file 12`.

**Private drafts.**

| Journal | Who sees it and its effect on the P&L |
|---|---|
| Your Draft | Only you |
| iLA's Draft (labour, accruals) | Everyone in the organisation |
| To be posted | Everyone |

Changing one of iLA's drafts makes your own private copy; the others keep seeing iLA's until yours is To be posted,
when it takes its place. To show someone a draft, download it (every table in the console downloads to Excel).

## 4. How a journal leaves the bucket

| What happens | Result |
|---|---|
| You import the file into Xero | On the next refresh iLA finds the OJ number in Xero's narration and removes it from the bucket. `posted 12` does it by hand if the narration was changed. |
| Someone posts their own journal for the same thing (e.g. the accountant's FNE payroll journal) | iLA's draft for it is removed; the labour view still shows iLA against Xero by account and department. |
| You delete a draft (`drop`) | Removed. |

Removed journals aren't shown anywhere in the bucket or the views. The audit log keeps the record (what it was, why
it left); there is no list of them.

## 5. Composing: `/OJ`

`/OJ --Oct 2026` opens the month: every journal in the bucket for October and the P&L with them on top. While it's
open, plain typing is read as journal commands (as in `/review`); `done` leaves. Everything is saved as you type.

**Play**

| Type | What happens |
|---|---|
| `new Wages accrual Oct` | New Draft dated the month end |
| `dr 6345 1200 fb DYN stub wages` | Debit 6345 1,200.00, Departments = Food and Beverage Services, Event = 261030DYN, description |
| `dr 6124 110 gst` | A line with a tax rate (`gst`, or `tax <rate name>`); otherwise BAS Excluded |
| `cr 2350` | No amount = the balancing figure |
| `6345 1200 to 2350` | Both sides in one line |
| `move 500 from 6345 to 6346` | Reclass |
| `rev` · `rev 15/11` · `rev off` | Reverses on day 1 of the next month, or on the date given; off again. Without it a journal doesn't reverse |
| `copy 12` · `reverse 12` | Start from another journal |

Accounts by code or part of the name; departments and events by code or the start of the name; if several match,
iLA lists them and asks.

**Tweak**

| Type | What happens |
|---|---|
| `3 1500` · `3 6346` · `3 event TOW` · `3 dept ov` · `3 desc …` | Change line 3 |
| `del 3` · `date 31/10` · `narr …` | Delete a line; journal date; narration |
| `undo` · `redo` · `versions` · `restore 2` | Step through every change, or go back to any version |
| `open 12` · `drop` | Work on another journal (yours or iLA's); delete this one |

**Listen**

After every change iLA shows the journal and the accounts it touches (Xero → after), and the effect on gross profit,
EBITDA and net profit.

| Type | What it shows |
|---|---|
| `pl` | P&L by group: Xero · To be posted · Draft · After |
| `pl lab` · `pl event` · `bs` · `tb` | A group's accounts; by event; balance sheet; trial balance |
| `mute 12` · `solo 12` · `unmute` | Leave a journal out of the view, or hear only one |
| `list` | The month's bucket: number with its suffix, made by, total, effect on net profit, and a **Generate Xero file** or **Back to draft** button on each row |
| `why 6345` | The Xero lines and overlay lines behind a figure |

**Finish**

| Type (or the button beside the journal) | What happens |
|---|---|
| `file` · `file 12` · `file all` — **Generate Xero file** | Checks the journal; builds its import file, ready to download (stamped, in the files register); the journal becomes To be posted (`-p1`) |
| `back 12` — **Back to draft** | To be posted → Draft; its file is cancelled. Generate again when ready (`-p2`) |
| `drop` — **Delete** | Removes the journal, in either status |
| `posted 12` | Remove from the bucket by hand, if iLA can't find it in Xero |

- There is no separate "ready" step: generating the file is the decision.
- A To be posted journal can't be changed; take it back to Draft first, so the file and the journal never differ.
- `file all` puts every Draft of yours for the month into one file; each journal in it becomes To be posted.
- Checks before the file is built: balanced; accounts in the chart; tracking options exist in Xero; month open. If
  one fails, iLA says which and the journal stays Draft.

## 6. Screen: the live panel

While `/OJ` is open the console shows two panels side by side, and the command line stays at the bottom. Both update
in place after every command; nothing scrolls away.

```
┌ OJ · October 2026 · TECC ───────────────────────────────────────────────────────────────┐  frozen
│ Net profit  Xero 41,300 · To be posted (2,100) · Draft (4,200) · After 35,000   3 journals │  headline
├──────────────────────────────────────────┬───────────────────────────────────────────────┤
│ OJ-0010 Draft · 31/10/2026 · Wages true-up │ P&L      Xero   To be posted   Draft    After │  frozen
│ #  Account          Dept  Event   Dr    Cr │                                               │  column heads
├──────────────────────────────────────────┼───────────────────────────────────────────────┤
│ 1  6345 Salaries…   F&B   DYN  4,200       │ Revenue        508,124     –       –   508,124 │
│ 2  2350 Payroll Cl…             4,200      │ Cost of sales   27,653     –       –    27,653 │  scrolls
│ balanced                                   │ Labour         126,560  2,100   4,200  132,860 │
│                                            │ …                                              │
└──────────────────────────────────────────┴───────────────────────────────────────────────┘
> _
```

- **Left:** the journal you're on (or `list`: the month's bucket).
- **Right:** the P&L with the journals on top; `pl lab`, `pl event`, `bs`, `tb` and `why` change what it shows. Rows a
  journal touches are highlighted.
- **Frozen:** the headline (month, the net profit line, how many journals) and each panel's column headings stay in
  place while the rows scroll.
- On a narrow screen the panels stack, journal first.

**General rule from now on, for every console view:** the headline and the column headings of a table stay frozen
while its rows scroll.

## 7. Elsewhere

- `/pl`, `/bs`, `/tb`, `/review`: `--landing` = Xero + To be posted + Draft (`--to-post` leaves Drafts out).
- `/status`: the bucket for the month, counts and totals by status.
- Admin: one page, **Overlay journals**, list and history. The Expectation Ledger page becomes the accruals working
  paper only.

## 8. Build steps

1. One bucket: two statuses, OJ numbers, private drafts, history; labour's journals moved onto it; the other
   statuses removed.
2. `/OJ` with the live panel (frozen headline and column headings): play, tweak, the P&L beside it.
3. Listen: `pl lab`, `pl event`, `bs`, `tb`, mute and solo, `list`, `why`.
4. Finish: generate the file (Draft → To be posted), back to draft, leaving the bucket on refresh, reversals.
5. `/accrue` puts its journal in the bucket; the Expectation Ledger stops holding journals.
6. `--landing` on the reports and `/status`; frozen headings on the existing console tables.

## 9. Decisions (21 November 2026)

| Question | Decided |
|---|---|
| Statuses | Draft and To be posted only; one bucket for every journal outside Xero |
| After leaving the bucket | Audit log only |
| Names | `/OJ`, OJ-0012 (`/jnl` and "journal" kept for later) |
| Sharing | Your drafts are private; download to share |
| Screen | Live P&L panel from the start; headline and column headings frozen, as the rule for all later work |
| Draft → To be posted | By generating the Xero import file (button or `file`); back to Draft or delete by hand |
| Numbering | Number on creation, never reused; suffix `-d`, `-p1`, `-p2` … shows the status and which file |
| iLA's drafts | Seen by everyone; changing one makes your private copy, which replaces it once To be posted |
| Existing ADJ numbers | Become OJ with the same numbers |
| Reversals | Never unless you say `rev`; day 1 of the next month by default, or the date you give |
| GST | BAS Excluded unless you say otherwise |

## 10. As built (21 November 2026)

Build steps 1 to 4 are done. Differences from the design above:

- Changing a line's account is `3 acc 6346` (`3 6346` would be read as an amount).
- Tax: `gst`, `gstfree` or `bas` on a line, or `3 tax <rate name>`.
- There is no "month open" check yet, because iLA has no month lock yet.
- `copy 12 to Nov` isn't built: `copy 12` copies into the month that's open.
- Not built yet: `pl event` and `tb` in the panel; build steps 5 and 6 (`/accrue` into the bucket; `--landing` on
  the reports and `/status`).

## 11. Added after the first build (21 November 2026)

- **Comparisons are a separate report, not in the panel:** `/pl --vs lm|ly|budget`, with `--landing` for the
  journals on top (on by default while `/OJ` is open; `--xero` turns it off). Last month's column is Xero's actuals.
- **Plain English in `/OJ`:** the language model turns a sentence into the short commands; iLA runs them, all or
  none. Accrue credits the accrual account and prepay debits the prepayment account (OJ settings). Accounts come
  from memory first (journals whose file was generated, kept in OJ memory), then similar journals in Xero, then the
  chart. The model can't generate files, delete or mark as posted.

