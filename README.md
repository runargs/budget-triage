# Triage — Budget Dashboard

A single-file, self-hosted envelope budgeting dashboard. Drop your transaction export and a few CSV files in a folder, run one Python script, open the generated HTML. No server, no account, no sync.

---

## How it works

This small app tells you the difference between money you *have* and money that's already spoken for. **Triage** is a local-first, open-source budget dashboard that runs entirely on your machine — no subscriptions, no sending your financial data to someone else's server. Your data stays wherever you keep it. Run one Python script, get a single HTML file.

Because it's just Python reading files, the data layer is 100% swappable. Point it at a database, wire up a Plaid fetch, pipe in a bank API etc. It's inspired by YNAB, Mint, Monarch, and other tools but it fills a gap I felt still existed. This is not an 'get out of debt' tool, it's not a pure backwards net worth log either, it's an action-oriented, active, rolling-forward intelligent budget methodology. A transparent, hackable tool for people who want to see exactly where their numbers come from.
---

## Quickstart

### 1. Install dependencies

```bash
pip install pandas numpy
```

### 2. Put your files together

Place the following in the same directory as `budget_dashboard_demo.py`:

| File | What it is |
|---|---|
| `ALL_TRX.csv` | Transaction export from your bank |
| `budgets.csv` | Monthly envelope allocations |
| `balances.csv` | Occasional bank balance snapshots |
| `transfers.csv` | Manual moves between envelopes |

### 3. Configure

Open `budget_dashboard_demo.py` and edit the configuration block at the top:

```python
BUDGET_START_DATE        = "2026-02-01"   # ignore transactions before this date
EXPECTED_MONTHLY_INCOME  = 5245.00        # your monthly take-home pay
INCOME_CATEGORIES        = {"Wages"}      # category name(s) for your paycheck
```

### 4. Run

```bash
python3 budget_dashboard_demo.py
```

This generates `budget_dashboard.html`. Open it in your browser.

---

## Demo

A full demo dataset is included — four months of realistic transactions for a fictional 28-year-old (Nov 2025 – Mar 2026, with March as a planning month). To run it:

```bash
python3 budget_dashboard_demo.py
```

---

## CSV formats

### `ALL_TRX.csv`

Transaction export from your bank or Plaid aggregator. Required columns:

| Column | Notes |
|---|---|
| `Posted Date` | Date transaction cleared, e.g. `2026-02-14` |
| `Authorized Date` | Used as fallback if Posted Date is missing |
| `Amount` | Positive = money in, negative = money out |
| `Status` | Only rows with `Posted` are processed |
| `Description` | Merchant name or description |
| `Detailed Category` | Maps to your envelope names |
| `Primary Category` | Used for health exclusions and income matching |

Triage matches transactions to envelopes using `Detailed Category`, so your category names in `ALL_TRX.csv` need to match the envelope names in `budgets.csv` exactly.

---

### `budgets.csv`

One row per envelope per month. Any envelope not listed for a given month gets zero new funding (but still carries its rollover balance forward).

```
month,category,allocated
2026-02,Rent,1650
2026-02,Groceries,320
2026-02,Restaurants & bars,260
```

---

### `balances.csv`

Occasional real bank balance snapshots — used to ground the dashboard in reality. You don't need to update this every month. The row for `2026-02` represents the balance *entering* February (i.e. your end-of-January statement balance).

```
month,balance
2026-01,5640.00
2026-02,5980.00
```

---

### `transfers.csv`

Manual reallocation between envelopes within a month — when you overspend dining and need to pull from retail, for example.

```
month,from_category,to_category,amount,note
2026-02,Clothing & accessories,Events & recreation,40.00,skipped shopping
```

---

## Configuration reference

```python
# File paths
TRX_CSV       = "ALL_TRX.csv"
BUDGETS_CSV   = "budgets.csv"
TRANSFERS_CSV = "transfers.csv"
BALANCES_CSV  = "balances.csv"
OUTPUT_HTML   = "budget_dashboard.html"

# Only process transactions on or after this date
BUDGET_START_DATE = "2026-02-01"

# Expected monthly take-home (used in income tracking and planning mode)
EXPECTED_MONTHLY_INCOME = 5245.00

# Detailed or Primary Category values that identify your paycheck
# These are separated from "other income" (refunds, side income, etc.)
INCOME_CATEGORIES = {"Wages"}

# Envelopes excluded from Operating Margin calculation
# Use for irregular or lumpy categories that would skew the health signal
HEALTH_EXCLUSIONS = {"Travel", "Other", "General", "Car services"}

# Transactions to silently ignore (not flagged as uncategorized)
UNTRACKED_EXCLUSIONS = {"Credit card payments", "Transfers"}

# Recurring envelopes — skipped by Smart Rebalance, carried forward each month
RECURRING_ENVELOPES = {"Rent", "Phone & internet", "Student loan payments", ...}

# Variable envelopes — show spend coverage % and end-of-month projection
VARIABLE_ENVELOPES = {"Groceries", "Restaurants & bars", "Gas & EV charging", ...}
```

---

## Dashboard reference

### Current month

**Income bar** — tracks posted wages vs expected, flags other income (refunds, side deposits) separately with its own sparkline. Remaining income uses a ±$0.05 buffer to handle paycheck rounding. If a balance snapshot exists, shows Unassigned Cash — real bank balance minus all enveloped funds.

**Operating Margin** — net surplus across all active envelopes (excluding health exclusions). The headline number for how your month is going.

**Deployed Capital** — total positive cash sitting in operational envelopes.

**Needs Attention** — combined shortfall across all underfunded envelopes.

**Envelope table** — all envelopes grouped by primary category, showing budgeted, spent, and available. Click any row to expand a transaction ledger. Variable envelopes show a spend coverage bar and end-of-month projection based on current pace.

**Trend sparklines** — each envelope shows a small historical chart of its available balance across all months.

**Uncategorized spend notice** — flags any transactions that posted but didn't match a known envelope, so nothing falls through the cracks.

---

### Planning mode (future months)

Navigate forward to any month that has entries in `budgets.csv` to enter planning mode.

**Unassigned Cash (Proj.)** — the grounded planning number. Calculated as:
```
last balance snapshot
+ all transactions since that snapshot
+ expected income this month
− all envelope rollover balances entering the month
− new allocations for this month
```
This is the cash you'd expect to see sitting outside all envelopes at end of month if everything goes to plan.

**Unallocated Surplus** — how much of this month's expected income hasn't been assigned to an envelope yet. Use this to decide where to put remaining budget.

**Deployed Capital** — total across all envelopes entering the month, including rollovers.

---

### What-if mode

Toggle **What-if** to simulate transfers without committing them. Queue moves between envelopes and see live updated balances. When you're happy, copy the queue as CSV to paste into `transfers.csv`.

### Smart Rebalance

One-click: finds all underfunded envelopes and automatically proposes transfers from envelopes with surplus to cover them, excluding recurring envelopes. Results go into the transfer queue for review.

---

## Tips

**Keep `BUDGET_START_DATE` current.** Set it to the first of the month your budgeting begins. Transactions before this date are ignored, which keeps old history from polluting your envelope balances.

**Update `balances.csv` monthly.** You don't have to, but a fresh snapshot once a month keeps Unassigned Cash and Projected Surplus accurate. Without it, those numbers are estimated from transaction math alone.

**Recurring envelopes don't need to be in every month's `budgets.csv`.** If Rent is `$1,650` in February, it stays `$1,650` going forward until you change it — Triage carries the last known allocation forward automatically for current and past months.

**Credit card payments should be in `UNTRACKED_EXCLUSIONS`.** Otherwise every payment looks like a double-spend. Your actual purchases are already tracked via their individual transaction rows.

**Category names are case-sensitive.** `Groceries` and `groceries` are different envelopes.
