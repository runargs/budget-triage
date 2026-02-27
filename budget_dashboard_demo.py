import pandas as pd
import numpy as np
import json
import os
from datetime import datetime

# ==========================================
# CONFIGURATION
# ==========================================
TRX_CSV       = "ALL_TRX.csv"
BUDGETS_CSV   = "budgets.csv"
TRANSFERS_CSV = "transfers.csv"
BALANCES_CSV  = "balances.csv"
OUTPUT_HTML   = "budget_dashboard.html"
BUDGET_START_DATE = "2025-11-01"
EXPECTED_MONTHLY_INCOME = 5245.00  # Set your expected monthly take-home here, e.g. 15000
INCOME_CATEGORIES = {"Wages"}   # e.g. {"Paycheck", "Direct Deposit"} — match your CSV category names exactly

# Primary categories to exclude from Net Health (Triage)
HEALTH_EXCLUSIONS = {"Travel", "Other", "General", "Car services"}
UNTRACKED_EXCLUSIONS = {"Credit card payments", "Transfers"}

# Envelopes that recur every month — skipped by smart rebalance, suggested forward
RECURRING_ENVELOPES = {"Rent", "Music & audio", "Home insurance", "Auto insurance", "Medical", "Student loan payments", "Investment transfers", "Other entertainment", "Fitness", "Games", "Phone & internet", "Other travel", "Emergency fund", "Car services"}

# Envelopes with variable spend — show coverage % and EOM projection
VARIABLE_ENVELOPES = {"Restaurants & bars", "Groceries", "Clothing & accessories", "Events & recreation", "Gas & EV charging", "Coffee shops", "Other food & drink", "Personal care", "Retail", "Pet supplies"}

def load_transactions():
    if not os.path.exists(TRX_CSV): return pd.DataFrame(), {}
    df = pd.read_csv(TRX_CSV)
    df.columns = df.columns.str.strip()
    df["date"] = pd.to_datetime(df["Posted Date"].fillna(df["Authorized Date"]))
    df["year_month"] = df["date"].dt.to_period("M").astype(str)
    df["amount_raw"] = pd.to_numeric(df["Amount"].astype(str).str.replace(r"[\$,\s]", "", regex=True), errors='coerce').fillna(0)
    df = df[df["Status"].str.strip().str.lower() == "posted"].copy()
    df["expense"] = df["amount_raw"].apply(lambda x: abs(x) if x < 0 else 0)
    df["refund"] = df["amount_raw"].apply(lambda x: x if x > 0 else 0)
    cat_mapping = dict(zip(df["Detailed Category"], df["Primary Category"]))
    return df, cat_mapping

def load_csv_safely(filepath, columns):
    if os.path.exists(filepath): return pd.read_csv(filepath)
    return pd.DataFrame(columns=columns)

def process_data():
    df_trx_all, cat_mapping = load_transactions()
    df_budgets = load_csv_safely(BUDGETS_CSV, ["month", "category", "allocated"])
    df_balances = load_csv_safely(BALANCES_CSV, ["month", "balance"])
    df_transfers = load_csv_safely(TRANSFERS_CSV, ["month", "from_category", "to_category", "amount", "note"])

    current_month_str = datetime.now().strftime("%Y-%m")
    start_dt = pd.to_datetime(BUDGET_START_DATE)
    df_trx = df_trx_all[df_trx_all["date"] >= start_dt].copy() if not df_trx_all.empty else df_trx_all.copy()

    planned_months = []
    if not df_budgets.empty: planned_months += df_budgets["month"].unique().tolist()
    if not df_transfers.empty: planned_months += df_transfers["month"].unique().tolist()

    months_set = set([current_month_str] + planned_months)
    if not df_trx.empty: months_set.update(df_trx["year_month"].unique())
    months_sorted = sorted(list(months_set))

    running_allocations, rollovers = {}, {}
    last_known_balance = 0
    last_snapshot_month = None

    # Pre-compute: for each month, sum of all transactions since the most recent
    # balances.csv snapshot (used to ground planning-mode projected surplus)
    # We'll update this incrementally as we iterate months.
    cumulative_txns_since_snapshot = 0.0  # resets when a new snapshot is seen
    dashboard_data = {"months": months_sorted, "current_month": current_month_str, "data_by_month": {}}
    budgeted_cats = sorted(list(set(df_budgets["category"].unique())))

    # Track available balance per category across all months for sparklines
    cat_history = {cat: [] for cat in budgeted_cats}

    for m in months_sorted:
        is_future = m > current_month_str
        month_data = {"summary": {}, "categories": {}, "is_future": is_future}

        if not df_balances.empty:
            bal_row = df_balances[df_balances["month"] == m]
            if not bal_row.empty:
                last_known_balance = float(bal_row.iloc[-1]["balance"])
                last_snapshot_month = m
                cumulative_txns_since_snapshot = 0.0  # reset — new anchor

        m_trx_all = df_trx_all[df_trx_all["year_month"] == m] if not df_trx_all.empty else pd.DataFrame()
        _inc = m_trx_all[m_trx_all["amount_raw"] > 0]["amount_raw"].sum() if not m_trx_all.empty else 0
        _exp = m_trx_all[m_trx_all["amount_raw"] < 0]["amount_raw"].abs().sum() if not m_trx_all.empty else 0
        live_bank_balance = round(last_known_balance + _inc - _exp, 2)
        # Accumulate all real transactions since last snapshot (non-future months only)
        if not is_future:
            cumulative_txns_since_snapshot += round(_inc - _exp, 2)
        m_transfers = df_transfers[df_transfers["month"] == m]
        t_in, t_out = {c: 0 for c in budgeted_cats}, {c: 0 for c in budgeted_cats}
        for _, r in m_transfers.iterrows():
            f, t, a = r["from_category"], r["to_category"], round(float(r["amount"]), 2)
            if f in t_out: t_out[f] += a
            if t in t_in: t_in[t] += a

        tot_health_pos = 0; tot_holes = 0; new_funding_total = 0; total_available_cash = 0
        m_trx_env = df_trx[df_trx["year_month"] == m] if not df_trx.empty else pd.DataFrame()

        # Sum of all envelope balances rolling INTO this month (before new allocations)
        total_rollover_into_month = round(sum(rollovers.get(cat, 0) for cat in budgeted_cats), 2)

        for cat in budgeted_cats:
            alloc_this_month = 0
            if not df_budgets.empty:
                v = df_budgets[(df_budgets["month"] == m) & (df_budgets["category"] == cat)]
                if not v.empty:
                    alloc_this_month = round(float(v.iloc[0]["allocated"]), 2)
                    running_allocations[cat] = alloc_this_month

            new_funding_total += alloc_this_month
            # For future months: only count explicitly allocated amounts.
            # For past/current months: fall back to last known allocation so
            # mid-month the envelope still reflects what was set at month start.
            if is_future:
                alloc = alloc_this_month
            else:
                alloc = running_allocations.get(cat, 0)
            roll = rollovers.get(cat, 0)
            assigned = round(alloc + t_in.get(cat, 0) - t_out.get(cat, 0), 2)
            total_budgeted = round(roll + assigned, 2)

            spent, refunds = 0, 0
            if not m_trx_env.empty:
                cat_trx = m_trx_env[m_trx_env["Detailed Category"] == cat]
                spent, refunds = round(float(cat_trx["expense"].sum()), 2), round(float(cat_trx["refund"].sum()), 2)

            bal = round(total_budgeted - spent + refunds, 2)
            p_cat = cat_mapping.get(cat, "General")

            if cat not in HEALTH_EXCLUSIONS and p_cat not in HEALTH_EXCLUSIONS:
                if bal < -0.01:
                    tot_holes += abs(bal)
                else:
                    if is_future:
                        surplus = round(bal - alloc_this_month, 2)
                        if surplus > 0.01: tot_health_pos += surplus
                    else:
                        if bal > 0.01: tot_health_pos += bal
            total_available_cash += bal

            rollovers[cat] = bal
            cat_history[cat].append({"month": m, "available": bal})

            ledger = []
            for _, tr in m_transfers[(m_transfers["from_category"] == cat) | (m_transfers["to_category"] == cat)].iterrows():
                is_out = tr["from_category"] == cat
                ledger.append({"date": m, "desc": f"To {tr['to_category']}" if is_out else f"From {tr['from_category']}", "amt": -float(tr["amount"]) if is_out else float(tr["amount"]), "note": str(tr.get("note", "") or "")})

            if not m_trx_env.empty:
                cat_trx = m_trx_env[m_trx_env["Detailed Category"] == cat]
                for _, tx in cat_trx.iterrows():
                    ledger.append({"date": str(tx["Posted Date"]), "desc": tx["Description"], "amt": tx["amount_raw"], "note": ""})

            # MoM delta: compare to previous month's available for this cat
            prev_hist = cat_history[cat]
            prev_available = prev_hist[-1]["available"] if prev_hist else None

            is_recurring = cat in RECURRING_ENVELOPES
            is_variable  = cat in VARIABLE_ENVELOPES

            if p_cat not in month_data["categories"]: month_data["categories"][p_cat] = []
            month_data["categories"][p_cat].append({
                "name": cat, "budgeted": total_budgeted, "spent": spent, "available": bal,
                "prev_available": prev_available,
                "is_recurring": is_recurring, "is_variable": is_variable,
                "ledger": ledger
            })

        # Uncategorized spend: transactions with no matching budgeted envelope
        uncategorized_spend = 0.0
        uncategorized_cats = {}
        if not m_trx_env.empty:
            for _, tx in m_trx_env[m_trx_env["expense"] > 0].iterrows():
                dc = tx.get("Detailed Category", "")
                pc = tx.get("Primary Category", "")
                if dc not in budgeted_cats and dc not in UNTRACKED_EXCLUSIONS and pc not in UNTRACKED_EXCLUSIONS:
                    uncategorized_spend += float(tx["expense"])
                    uncategorized_cats[dc] = uncategorized_cats.get(dc, 0) + float(tx["expense"])

        # Posted income = positive transactions matching configured income categories
        # Other income = positive transactions NOT in income categories and NOT in untracked exclusions
        if not m_trx_all.empty and INCOME_CATEGORIES:
            income_mask = (
                m_trx_all["amount_raw"] > 0
            ) & (
                m_trx_all.get("Primary Category", pd.Series(dtype=str)).isin(INCOME_CATEGORIES) |
                m_trx_all.get("Detailed Category", pd.Series(dtype=str)).isin(INCOME_CATEGORIES)
            )
            posted_income = round(float(m_trx_all[income_mask]["amount_raw"].sum()), 2)
            # Other income: positive txns not matching wages and not excluded
            excl = INCOME_CATEGORIES | UNTRACKED_EXCLUSIONS
            other_income_mask = (
                m_trx_all["amount_raw"] > 0
            ) & ~(
                m_trx_all.get("Primary Category", pd.Series(dtype=str)).isin(excl) |
                m_trx_all.get("Detailed Category", pd.Series(dtype=str)).isin(excl)
            )
            other_income = round(float(m_trx_all[other_income_mask]["amount_raw"].sum()), 2)
        elif not m_trx_all.empty:
            # No filter set — fall back to all positive transactions
            posted_income = round(float(m_trx_all[m_trx_all["amount_raw"] > 0]["amount_raw"].sum()), 2)
            other_income = 0.0
        else:
            posted_income = 0.0
            other_income = 0.0

        # Snapshot balance from balances.csv for this month (None if not set)
        snapshot_balance = None
        if not df_balances.empty:
            bal_snap = df_balances[df_balances["month"] == m]
            if not bal_snap.empty:
                snapshot_balance = round(float(bal_snap.iloc[-1]["balance"]), 2)

        month_data["summary"] = {
            "bank": live_bank_balance,
            "last_snapshot": round(last_known_balance, 2),
            "last_snapshot_month": last_snapshot_month,
            "txns_since_snapshot": round(cumulative_txns_since_snapshot, 2),
            "rollover_into_month": total_rollover_into_month,
            "excess": round(tot_health_pos, 2),
            "holes": round(tot_holes, 2),
            "net_health": round(tot_health_pos - tot_holes, 2),
            "new_funding": round(new_funding_total, 2),
            "total_enveloped": round(total_available_cash, 2),
            "posted_income": posted_income,
            "other_income": other_income,
            "expected_income": EXPECTED_MONTHLY_INCOME,
            "snapshot_balance": snapshot_balance,
            "uncategorized_spend": round(uncategorized_spend, 2),
            "uncategorized_cats": {k: round(v, 2) for k, v in sorted(uncategorized_cats.items(), key=lambda x: -x[1])},
        }
        dashboard_data["data_by_month"][m] = month_data

    dashboard_data["cat_history"] = cat_history
    dashboard_data["config"] = {
        "recurring": list(RECURRING_ENVELOPES),
        "variable": list(VARIABLE_ENVELOPES),
        "health_exclusions": list(HEALTH_EXCLUSIONS),
    }
    # Build income sparkline histories
    income_history = []
    other_income_history = []
    for m in months_sorted:
        s = dashboard_data["data_by_month"][m]["summary"]
        income_history.append({"month": m, "amount": s.get("posted_income", 0)})
        other_income_history.append({"month": m, "amount": s.get("other_income", 0)})
    dashboard_data["income_history"] = income_history
    dashboard_data["other_income_history"] = other_income_history
    return dashboard_data

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Triage — Budget Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,300;1,400&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg:        #18160f;
            --bg2:       #1e1b13;
            --surf:      #242017;
            --surf2:     #2c2820;
            --surf3:     #332f22;
            --border:    #3a3526;
            --border2:   #4a4436;
            --text:      #ede5d0;
            --text2:     #b8a98a;
            --muted:     #7a6f5a;
            --green:      #5a9466;  /* was #7aab82 */
            --green-dim:  #3a6045;  /* was #4a7052 */
            --green-soft: #5a944618; /* was #7aab8218 */
            --sage:      #7aab82;
            --sage-dim:  #4a7052;
            --sage-soft: #7aab8218;
            --rose:      #a07880;
            --rose-dim:  #6e4e55;
            --rose-soft: #a0787820;
            --planning:  #3a4a5e;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: var(--bg);
            color: var(--text);
            font-family: 'DM Mono', monospace;
            min-height: 100vh;
            padding-bottom: 160px;
        }

        /* ── Layout ── */
        .container { max-width: 980px; margin: 0 auto; padding: 48px 24px; }

        /* ── Masthead ── */
        .masthead {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            margin-bottom: 40px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--border);
        }
        .wordmark {
            font-family: 'Cormorant Garamond', serif;
            font-size: 13px;
            font-weight: 300;
            letter-spacing: 4px;
            text-transform: uppercase;
            color: var(--muted);
        }
        .wordmark span { color: var(--green); }
        .month-nav { display: flex; align-items: center; gap: 16px; }
        .month-label {
            font-family: 'Cormorant Garamond', serif;
            font-size: 22px;
            font-weight: 400;
            letter-spacing: 1px;
            color: var(--text);
            min-width: 90px;
            text-align: center;
        }
        .nav-btn {
            background: none;
            border: 1px solid var(--border2);
            color: var(--muted);
            width: 30px; height: 30px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 16px;
            display: flex; align-items: center; justify-content: center;
            transition: all 0.15s ease;
        }
        .nav-btn:hover { border-color: var(--green); color: var(--green); }

        /* ── Planning badge ── */
        .planning-badge {
            display: none;
            font-size: 9px;
            letter-spacing: 2.5px;
            color: #8cb4ff;
            background: var(--planning);
            border: 1px solid #4a5a6e;
            padding: 4px 12px;
            border-radius: 2px;
            margin-bottom: 24px;
        }
        .disclaimer {
            display: none;
            font-size: 11px;
            color: var(--muted);
            font-style: italic;
            background: var(--bg2);
            padding: 12px 16px;
            border-left: 2px solid var(--green-dim);
            margin-bottom: 24px;
        }

        /* ── Triage summary ── */
        .triage-grid {
            display: grid;
            grid-template-columns: 2fr 1fr 1fr;
            gap: 16px;
            margin-bottom: 32px;
        }
        /* Planning mode: small unassigned cash strip above main triage grid */
        .planning-strip {
            display: none;
            background: var(--surf);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px 24px;
            margin-bottom: 16px;
            position: relative;
            overflow: hidden;
        }
        .planning-strip::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 2px;
            background: linear-gradient(90deg, var(--green), transparent);
        }
        .planning-strip-inner {
            display: flex;
            align-items: baseline;
            gap: 16px;
            flex-wrap: wrap;
        }
        .planning-strip-label {
            font-size: 9px;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: var(--muted);
        }
        .planning-strip-value {
            font-family: 'Cormorant Garamond', serif;
            font-size: 28px;
            font-weight: 300;
            line-height: 1;
        }
        .planning-strip-sub {
            font-size: 10px;
            color: var(--muted);
            flex: 1;
        }
        .planning-strip-breakdown {
            display: flex;
            gap: 0;
            flex: 1;
            justify-content: flex-end;
        }
        .planning-strip-item {
            padding: 0 16px;
            border-right: 1px solid var(--border);
            text-align: right;
        }
        .planning-strip-item:last-child { border-right: none; padding-right: 0; }
        .planning-strip-item .psi-label { font-size: 9px; color: var(--muted); margin-bottom: 2px; }
        .planning-strip-item .psi-val {
            font-family: 'Cormorant Garamond', serif;
            font-size: 15px;
            font-weight: 300;
        }
        .triage-card {
            background: var(--surf);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 24px;
            position: relative;
            overflow: hidden;
        }
        .triage-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 2px;
        }
        .triage-card.primary::before { background: linear-gradient(90deg, var(--green), transparent); }
        .triage-card.card-holes::before  { background: linear-gradient(90deg, var(--rose-dim), transparent); }
        .triage-card.card-excess::before { background: linear-gradient(90deg, var(--sage-dim), transparent); }

        .card-label {
            font-size: 9px;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 12px;
        }
        .card-value {
            font-family: 'Cormorant Garamond', serif;
            font-size: 42px;
            font-weight: 300;
            line-height: 1;
            margin-bottom: 6px;
        }
        .card-value.cv-pos { color: var(--sage); }
        .card-value.cv-neg { color: var(--rose); }
        .card-value.cv-green { color: var(--green); }
        .card-sub { font-size: 10px; color: var(--muted); line-height: 1.5; }
        .decimal { font-size: 0.55em; opacity: 0.7; vertical-align: baseline; letter-spacing: 0; }
        .card-value-md {
            font-family: 'Cormorant Garamond', serif;
            font-size: 30px;
            font-weight: 300;
            line-height: 1;
            margin-bottom: 6px;
        }

        /* ── Toolbar ── */
        .toolbar {
            display: flex;
            gap: 12px;
            align-items: center;
            margin-bottom: 24px;
        }
        .search-wrap { flex: 1; position: relative; }
        .search-icon {
            position: absolute;
            left: 12px; top: 50%;
            transform: translateY(-50%);
            color: var(--muted);
            font-size: 14px;
            pointer-events: none;
        }
        input[type="text"], input[type="number"], select {
            background: var(--surf);
            color: var(--text);
            border: 1px solid var(--border);
            padding: 10px 12px;
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            border-radius: 4px;
            outline: none;
            transition: border-color 0.15s;
        }
        input[type="text"]:focus, input[type="number"]:focus, select:focus {
            border-color: var(--green-dim);
        }
        .search-input { width: 100%; padding-left: 34px; }
        ::placeholder { color: var(--muted); }

        .export-btn {
            background: none;
            border: 1px solid var(--border2);
            color: var(--text2);
            padding: 10px 16px;
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 1px;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .export-btn:hover { border-color: var(--green-dim); color: var(--green); }

        /* ── Table ── */
        table { width: 100%; border-collapse: collapse; }
        thead th {
            text-align: right;
            font-size: 9px;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: var(--muted);
            padding: 0 16px 12px;
            border-bottom: 1px solid var(--border);
            font-weight: 400;
        }
        thead th:first-child { text-align: left; }
        thead th.th-spark { text-align: center; }

        .group-header td {
            background: var(--bg2);
            font-size: 9px;
            letter-spacing: 2.5px;
            text-transform: uppercase;
            color: var(--text);
            padding: 10px 16px;
            border-bottom: 1px solid var(--border);
        }

        .cat-row td {
            padding: 14px 16px;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
            text-align: right;
        }
        .cat-row td:first-child { text-align: left; }
        .cat-row td.td-spark { text-align: center; }
        .cat-row { transition: background 0.1s; }
        .cat-row:hover td { background: var(--surf2); }

        .cat-name {
            cursor: pointer;
            font-size: 13px;
            font-weight: 400;
            color: var(--text);
            transition: color 0.12s;
        }
        .cat-name:hover { color: var(--green); }

        /* Sparkline */
        svg.spark { display: block; }

        .amount-muted { font-size: 12px; color: var(--text2); }

        /* ── Pills ── */
        .pill {
            display: inline-block;
            padding: 5px 14px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: 500;
            min-width: 90px;
            text-align: right;
            cursor: pointer;
            transition: opacity 0.15s;
        }
        .pill:hover { opacity: 0.75; }
        .pill-pos  { background: var(--sage-soft);  color: var(--sage);  border: 1px solid var(--sage-dim); }
        .pill-green { background: var(--green-soft);  color: var(--green);  border: 1px solid var(--green-dim); }
        .pill-neg  { background: var(--rose-soft);  color: var(--rose);  border: 1px solid var(--rose-dim); }
        .pill-zero { background: transparent; color: var(--muted); border: 1px solid var(--border); }

        /* ── Ledger ── */
        .ledger-row-wrap { display: none; background: var(--bg); }
        .ledger-inner { padding: 0 16px 16px; }
        .ledger-header {
            display: grid;
            grid-template-columns: 90px 1.8fr 1fr 100px;
            padding: 10px 0 8px;
            font-size: 9px;
            letter-spacing: 1.5px;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
            margin-bottom: 4px;
        }
        .ledger-header span:last-child { text-align: right; }
        .ledger-item {
            display: grid;
            grid-template-columns: 90px 1.8fr 1fr 100px;
            padding: 8px 0;
            font-size: 11px;
            color: var(--text2);
            border-bottom: 1px solid var(--border);
        }
        .ledger-item span:last-child { text-align: right; }
        .amt-pos { color: var(--sage); }
        .amt-neg { color: var(--rose); }
        .truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 12px; }

        /* ── Footer ── */
        .footer {
            position: fixed;
            bottom: 0; left: 0;
            width: 100%;
            background: var(--surf);
            border-top: 1px solid var(--border2);
            z-index: 100;
        }
        .footer-inner { max-width: 980px; margin: 0 auto; padding: 0 24px; }

        .queue-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 0 0;
            cursor: pointer;
            user-select: none;
        }
        .queue-title {
            font-size: 9px;
            letter-spacing: 2px;
            color: var(--muted);
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .queue-count {
            background: var(--green);
            color: var(--bg);
            font-size: 9px;
            font-weight: 700;
            padding: 1px 6px;
            border-radius: 10px;
            display: none;
        }
        .queue-toggle-hint { font-size: 9px; color: var(--muted); }

        .transfer-form {
            display: flex;
            gap: 10px;
            align-items: center;
            padding: 10px 0 12px;
            flex-wrap: wrap;
        }
        .tf-label { font-size: 9px; letter-spacing: 1.5px; color: var(--muted); white-space: nowrap; }
        .transfer-form select { flex: 1.5; min-width: 130px; }
        .transfer-form input[type="number"] { width: 100px; }
        .transfer-form input[type="text"] { flex: 1; min-width: 120px; }

        .add-btn {
            background: var(--surf3);
            border: 1px solid var(--border2);
            color: var(--text2);
            padding: 10px 18px;
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 1px;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .add-btn:hover { border-color: var(--green-dim); color: var(--green); }

        .copy-all-btn {
            background: var(--green);
            border: none;
            color: var(--bg);
            padding: 10px 24px;
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.5px;
            border-radius: 4px;
            cursor: pointer;
            transition: opacity 0.15s;
        }
        .copy-all-btn:hover { opacity: 0.85; }
        .copy-all-btn:disabled { opacity: 0.35; cursor: default; }

        .queue-list { display: none; padding-bottom: 12px; }
        .queue-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 6px 0;
            font-size: 11px;
            color: var(--text2);
            border-bottom: 1px solid var(--border);
        }
        .queue-item-remove {
            background: none; border: none;
            color: var(--muted);
            cursor: pointer; font-size: 14px;
            padding: 0 4px; line-height: 1;
            transition: color 0.12s;
        }
        .queue-item-remove:hover { color: var(--rose); }

        /* ── Export modal ── */
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(10,8,5,0.85);
            z-index: 200;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.open { display: flex; }
        .modal {
            background: var(--surf);
            border: 1px solid var(--border2);
            border-radius: 8px;
            padding: 32px;
            width: 560px;
            max-width: 90vw;
            position: relative;
        }
        .modal h2 {
            font-family: 'Cormorant Garamond', serif;
            font-size: 24px;
            font-weight: 400;
            margin-bottom: 6px;
        }
        .modal-sub { font-size: 11px; color: var(--muted); margin-bottom: 20px; }
        .modal-close {
            position: absolute; top: 16px; right: 16px;
            background: none; border: none;
            color: var(--muted); font-size: 20px; cursor: pointer;
            transition: color 0.12s;
        }
        .modal-close:hover { color: var(--text); }
        .export-textarea {
            width: 100%; height: 240px;
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--text2);
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            padding: 14px;
            border-radius: 4px;
            resize: none; outline: none;
            line-height: 1.6;
        }
        .modal-actions {
            display: flex; gap: 10px;
            margin-top: 16px; justify-content: flex-end;
        }
        .modal-copy-btn {
            background: var(--green); border: none;
            color: var(--bg);
            padding: 10px 22px;
            font-family: 'DM Mono', monospace;
            font-size: 10px; font-weight: 700; letter-spacing: 1px;
            border-radius: 4px; cursor: pointer;
            transition: opacity 0.15s;
        }
        .modal-copy-btn:hover { opacity: 0.85; }
        .modal-cancel {
            background: none;
            border: 1px solid var(--border2);
            color: var(--muted);
            padding: 10px 18px;
            font-family: 'DM Mono', monospace;
            font-size: 10px; border-radius: 4px; cursor: pointer;
            transition: all 0.12s;
        }
        .modal-cancel:hover { color: var(--text); border-color: var(--text2); }

        /* ── Utility ── */
        .total-line {
            text-align: right;
            font-size: 10px;
            color: var(--muted);
            margin-bottom: 28px;
            padding-top: 8px;
        }
        .total-line span { color: var(--text2); }

        /* ── Sub-indicators under Available pill ── */
        .avail-cell { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
        .sub-indicators { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
        .sub-ind {
            font-size: 9px;
            letter-spacing: 0.5px;
            color: var(--muted);
            white-space: nowrap;
        }
        .sub-ind .ind-label { color: var(--muted); margin-right: 3px; }
        .sub-ind .ind-val-pos  { color: var(--sage); }
        .sub-ind .ind-val-neg  { color: var(--rose); }
        .sub-ind .ind-val-zero { color: var(--muted); }
        .sub-ind .ind-val-green { color: var(--green); }
        /* ── Income bar ── */
        .income-bar {
            display: flex;
            gap: 0;
            align-items: stretch;
            background: var(--surf);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 12px 20px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 0;
        }
        .income-bar.future { display: none; }
        .income-stat {
            flex: 1;
            min-width: 120px;
            padding: 0 16px;
            border-right: 1px solid var(--border);
        }
        .income-stat:first-child { padding-left: 0; }
        .income-stat:last-child { border-right: none; }
        .income-label {
            font-size: 9px;
            letter-spacing: 1.8px;
            text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 5px;
        }
        .income-value {
            font-family: 'Cormorant Garamond', serif;
            font-size: 20px;
            font-weight: 300;
            line-height: 1;
        }
        .income-sub {
            font-size: 9px;
            color: var(--muted);
            margin-top: 3px;
        }
        .progress-wrap {
            width: 100%;
            height: 2px;
            background: var(--border);
            border-radius: 1px;
            margin-top: 6px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            border-radius: 1px;
            transition: width 0.4s ease;
        }

        /* ── Planning mode unallocated breakdown ── */
        .card-breakdown { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 6px; }
        .breakdown-row { display: flex; justify-content: space-between; align-items: center; font-size: 10px; }
        .breakdown-label { color: var(--muted); }
        .breakdown-val { font-family: 'DM Mono', monospace; font-size: 11px; }

        /* ── What-if mode ── */
        body.whatif-active { --border: #4a4a2e; }
        .whatif-badge {
            display: none;
            font-size: 9px; letter-spacing: 2.5px;
            color: var(--green); background: #2a2810;
            border: 1px solid var(--green-dim);
            padding: 4px 12px; border-radius: 2px;
            margin-left: 8px;
        }
        body.whatif-active .whatif-badge { display: inline-block; }
        .toolbar-btn {
            background: none;
            border: 1px solid var(--border2);
            color: var(--text2);
            padding: 10px 14px;
            font-family: 'DM Mono', monospace;
            font-size: 10px; letter-spacing: 1px;
            border-radius: 4px; cursor: pointer;
            transition: all 0.15s; white-space: nowrap;
        }
        .toolbar-btn:hover { border-color: var(--green-dim); color: var(--green); }
        .toolbar-btn.active { background: #2a2810; border-color: var(--green-dim); color: var(--green); }

        /* ── Uncategorized notice ── */
        .uncat-line {
            font-size: 10px; color: var(--muted);
            margin-bottom: 6px; padding-top: 4px;
            cursor: pointer; display: none;
        }
        .uncat-line:hover { color: var(--text2); }
        .uncat-line span { color: var(--text2); }
        .uncat-detail {
            display: none;
            background: var(--bg2);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 10px 16px;
            margin-bottom: 16px;
            font-size: 10px;
        }
        .uncat-row {
            display: flex; justify-content: space-between;
            padding: 4px 0; border-bottom: 1px solid var(--border);
            color: var(--text2);
        }
        .uncat-row:last-child { border-bottom: none; }
        .uncat-row .uncat-cat { color: var(--muted); }

        /* ── Coverage indicator ── */
        .coverage-bar {
            width: 56px; height: 4px;
            background: var(--border);
            border-radius: 2px; overflow: hidden;
            margin: 4px 0 2px auto;
        }
        .coverage-fill { height: 100%; border-radius: 2px; transition: width 0.3s; }
        .coverage-label { font-size: 9px; color: var(--muted); text-align: right; }

        @keyframes fadeSlideIn {
            from { opacity: 0; transform: translateY(4px); }
            to   { opacity: 1; transform: none; }
        }
        .cat-row { animation: fadeSlideIn 0.2s ease both; }
    </style>
</head>
<body>
<div class="container">

    <div class="masthead">
        <div class="wordmark"><span>Triage</span> &nbsp;·&nbsp; Budget Dashboard</div>
        <div class="month-nav">
            <button class="nav-btn" onclick="changeMonth(-1)">&#8249;</button>
            <div class="month-label" id="monthLabel"></div>
            <button class="nav-btn" onclick="changeMonth(1)">&#8250;</button>
        </div>
    </div>

    <div class="planning-badge" id="planningBadge">PLANNING MODE</div>
    <div class="disclaimer" id="disclaimer">
        <strong>Planning Mode:</strong> Projected Surplus excludes new seed funding — showing only surplus rolling forward from today.
    </div>

    <div id="incomeBar"></div>
    <div class="planning-strip" id="planningStrip"></div>
    <div class="triage-grid" id="triageHeader"></div>

    <div class="toolbar">
        <div class="search-wrap">
            <span class="search-icon">&#9906;</span>
            <input type="text" class="search-input" id="search" placeholder="Search envelopes…" oninput="render()">
        </div>
        <button class="toolbar-btn" id="underfundedBtn" onclick="toggleSort()">Underfunded First</button>
        <button class="toolbar-btn" id="whatifBtn" onclick="toggleWhatif()">What-if <span class="whatif-badge" id="whatifBadge">ON</span></button>
        <button class="toolbar-btn" id="smartBtn" onclick="smartRebalance()">Smart Rebalance</button>
        <button class="export-btn" onclick="openExport()">Export CSV</button>
    </div>

    <div class="uncat-line" id="uncatLine" onclick="toggleUncatDetail()"></div>
    <div class="uncat-detail" id="uncatDetail"></div>

    <div id="tableBody"></div>
    <div class="total-line" id="totalLine"></div>
</div>

<!-- Footer transfer panel -->
<div class="footer">
    <div class="footer-inner">
        <div class="queue-header" onclick="toggleQueue()">
            <div class="queue-title">
                TRANSFERS
                <span class="queue-count" id="queueCount">0</span>
            </div>
            <div class="queue-toggle-hint" id="queueHint">&#9650; show queue</div>
        </div>
        <div class="transfer-form">
            <span class="tf-label">FROM</span>
            <select id="fromCat"><option value="">Select envelope</option></select>
            <span class="tf-label">TO</span>
            <select id="toCat"><option value="">Select envelope</option></select>
            <input type="number" id="amt" placeholder="0.00" step="0.01" min="0">
            <input type="text" id="note" placeholder="Reason…">
            <button class="add-btn" onclick="addToQueue()">+ Queue</button>
            <button class="copy-all-btn" id="copyAllBtn" onclick="copyQueue()" disabled>Copy All</button>
        </div>
        <div class="queue-list" id="queueList"></div>
    </div>
</div>

<!-- Export modal -->
<div class="modal-overlay" id="exportModal">
    <div class="modal">
        <button class="modal-close" onclick="closeExport()">&#215;</button>
        <h2>Export Month</h2>
        <div class="modal-sub">CSV snapshot for <span id="exportMonthLabel"></span> — copy and paste into your spreadsheet.</div>
        <textarea class="export-textarea" id="exportText" readonly></textarea>
        <div class="modal-actions">
            <button class="modal-cancel" onclick="closeExport()">Cancel</button>
            <button class="modal-copy-btn" id="modalCopyBtn" onclick="copyExport()">Copy to Clipboard</button>
        </div>
    </div>
</div>

<script>
const DB = __DATA__;
let curMonth = DB.current_month;
let transferQueue = [];
let queueOpen = false;
let catsPopulated = false;
let sortUnderfunded = false;
let whatifMode = false;
let whatifDeltas = {}; // catName -> delta amount from queued what-if transfers

// Days elapsed and remaining in current month
function monthProgress() {
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const elapsed = now.getDate();
    return { elapsed, total: daysInMonth, pct: elapsed / daysInMonth };
}

function fmt(n) {
    const s = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2 });
    const dot = s.lastIndexOf('.');
    if (dot === -1) return s;
    return s.slice(0, dot) + '<span class="decimal">' + s.slice(dot) + '</span>';
}

function fmtSigned(n) {
    return (n < 0 ? '−' : '') + '$' + fmt(n);
}

// ── Income sparkline builder ──
function buildIncomeSparkline(history, color) {
    if (!history || history.length < 2) return '';
    const vals = history.map(h => h.amount);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;
    const W = 56, H = 20, pad = 2;
    const n = vals.length;
    const xs = vals.map((_, i) => pad + (i / (n - 1)) * (W - pad * 2));
    const ys = vals.map(v => H - pad - ((v - min) / range) * (H - pad * 2));
    const pts = xs.map((x, i) => x.toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
    const lx = xs[n - 1].toFixed(1), ly = ys[n - 1].toFixed(1);
    return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:inline-block;vertical-align:middle">
        <polyline points="${pts}" fill="none" stroke="var(--border2)" stroke-width="1.2" stroke-linejoin="round"/>
        <circle cx="${lx}" cy="${ly}" r="2.2" fill="${color}"/>
    </svg>`;
}

// ── Sparkline builder ──
function buildSparkline(catName) {
    const history = DB.cat_history[catName];
    if (!history || history.length < 2) return '';
    const vals = history.map(h => h.available);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;
    const W = 56, H = 20, pad = 2;
    const n = vals.length;
    const xs = vals.map((_, i) => pad + (i / (n - 1)) * (W - pad * 2));
    const ys = vals.map(v => H - pad - ((v - min) / range) * (H - pad * 2));
    const pts = xs.map((x, i) => x.toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
    const last = vals[n - 1];
    const dotColor = last < -0.01 ? 'var(--rose)' : last > 100 ? 'var(--green)' : 'var(--sage)';
    const lx = xs[n - 1].toFixed(1), ly = ys[n - 1].toFixed(1);
    return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
        <polyline points="${pts}" fill="none" stroke="var(--border2)" stroke-width="1.2" stroke-linejoin="round"/>
        <circle cx="${lx}" cy="${ly}" r="2.2" fill="${dotColor}"/>
    </svg>`;
}

// ── Main render ──
function render() {
    const m = DB.data_by_month[curMonth];
    const filter = document.getElementById('search').value.toLowerCase();
    const prog = monthProgress();

    document.getElementById('monthLabel').textContent = curMonth;
    document.getElementById('planningBadge').style.display = m.is_future ? 'inline-block' : 'none';
    document.getElementById('disclaimer').style.display = m.is_future ? 'block' : 'none';

    // ── Income bar (current month only) ──
    const incomeBar = document.getElementById('incomeBar');
    if (m.summary.expected_income === 0) {
        incomeBar.innerHTML = '';
    } else if (m.is_future) {
        // Planning mode: show expected income + allocation status
        const expected = m.summary.expected_income;
        const allocated = m.summary.new_funding;
        // Unallocated = expected income minus what's been budgeted to envelopes this month
        const unallocated = expected - allocated;
        const uClass = unallocated < -0.01 ? 'var(--rose)' : unallocated > 0.01 ? 'var(--sage)' : 'var(--muted)';
        const uSign = unallocated < 0 ? '−' : '';
        incomeBar.innerHTML = '';
    } else {
        const posted   = m.summary.posted_income;
        const expected = m.summary.expected_income;
        const otherInc = m.summary.other_income || 0;
        // Buffer of ±$0.05 per paycheck — use a small tolerance for "remaining"
        const INCOME_BUFFER = 0.05;
        const remaining = expected - posted;
        const absRemaining = Math.abs(remaining);
        const isWithinBuffer = absRemaining <= INCOME_BUFFER;
        const allocated = m.summary.new_funding;
        const pct = Math.min(100, Math.round((posted / expected) * 100));
        const remainClass = remaining <= INCOME_BUFFER ? 'cv-pos' : 'cv-neg';
        const remainLabel = isWithinBuffer ? 'within paycheck buffer' : remaining <= 0 ? 'fully received' : 'still pending';
        const remainDisplay = isWithinBuffer ? `✓ ~$0` : `${remaining > 0 ? '' : '✓ '}$${fmt(absRemaining)}`;

        const incSpark = buildIncomeSparkline(DB.income_history, 'var(--sage)');
        const otherSpark = buildIncomeSparkline(DB.other_income_history, 'var(--green)');

        // Unassigned cash: actual bank balance minus ALL enveloped cash
        // total_enveloped already sums every envelope balance (including normally-excluded ones)
        const snap = m.summary.snapshot_balance;
        const unassignedStat = (snap !== null && snap !== undefined) ? (() => {
            const unassigned = m.summary.bank - m.summary.total_enveloped;
            const uClass = unassigned < -0.01 ? 'var(--rose)' : 'var(--sage)';
            const uSign = unassigned < 0 ? '−' : '';
            const uSub = unassigned < -0.01 ? 'over-allocated vs cash' : 'sitting outside envelopes';
            return `<div class="income-stat">
                    <div class="income-label">Unassigned Cash</div>
                    <div class="income-value" style="color:${uClass}">${uSign}$${fmt(Math.abs(unassigned))}</div>
                    <div class="income-sub">${uSub}</div>
                </div>`;
        })() : '';

        const otherStat = otherInc > 0.01 ? `
            <div class="income-stat">
                <div class="income-label">Other Income ${otherSpark}</div>
                <div class="income-value" style="color:var(--green)">$${fmt(otherInc)}</div>
                <div class="income-sub">non-wage deposits this month</div>
            </div>` : '';

        incomeBar.innerHTML = `
            <div class="income-bar">
                <div class="income-stat">
                    <div class="income-label">Posted Income (Wages) ${incSpark}</div>
                    <div class="income-value" style="color:var(--sage)">$${fmt(posted)}</div>
                    <div class="progress-wrap"><div class="progress-fill" style="width:${pct}%;background:var(--sage-dim)"></div></div>
                    <div class="income-sub">${pct}% of expected</div>
                </div>
                ${otherStat}
                <div class="income-stat">
                    <div class="income-label">Expected</div>
                    <div class="income-value" style="color:var(--text2)">$${fmt(expected)}</div>
                    <div class="income-sub">monthly target</div>
                </div>
                <div class="income-stat">
                    <div class="income-label">Remaining</div>
                    <div class="income-value ${remainClass}">${remainDisplay}</div>
                    <div class="income-sub">${remainLabel}</div>
                </div>
                ${unassignedStat}
            </div>`;
    }

    const nh = m.summary.net_health;
    const planningStrip = document.getElementById('planningStrip');

    let triageHTML;
    if (m.is_future) {
        const snap = m.summary.last_snapshot;
        const snapMonth = m.summary.last_snapshot_month;
        const txnsSince = m.summary.txns_since_snapshot || 0;
        const rollover = m.summary.rollover_into_month || 0;
        const currentBalance = snap + txnsSince;
        const totalSpokenFor = rollover + m.summary.new_funding;
        const projUnassigned = currentBalance + m.summary.expected_income - totalSpokenFor;

        let snapLabel = 'no balance snapshot on record';
        if (snapMonth) {
            const [sy, sm] = snapMonth.split('-').map(Number);
            const prevDate = new Date(sy, sm - 2, 1);
            const prevLabel = prevDate.toLocaleString('default', { month: 'long', year: 'numeric' });
            snapLabel = `EOM ${prevLabel}`;
        }
        const snapColor = snapMonth ? 'var(--text2)' : 'var(--rose)';
        const puClass = projUnassigned >= 0 ? 'cv-pos' : 'cv-neg';
        const puSign  = projUnassigned < 0 ? '−' : '';
        const txnsSign = txnsSince >= 0 ? '+' : '−';

        // Small strip: Unassigned Cash (Proj.) with breakdown
        planningStrip.style.display = 'block';
        planningStrip.innerHTML = `
            <div class="planning-strip-inner">
                <div>
                    <div class="planning-strip-label">Unassigned Cash (Proj.)</div>
                    <div class="planning-strip-value ${puClass}">${puSign}$${fmt(Math.abs(projUnassigned))}</div>
                </div>
                <div class="planning-strip-sub">free cash after all obligations</div>
                <div class="planning-strip-breakdown">
                    <div class="planning-strip-item">
                        <div class="psi-label">${snapLabel}</div>
                        <div class="psi-val" style="color:${snapColor}">$${fmt(snap)}</div>
                    </div>
                    ${Math.abs(txnsSince) > 0.01 ? `
                    <div class="planning-strip-item">
                        <div class="psi-label">+ txns since snapshot</div>
                        <div class="psi-val" style="color:${txnsSince >= 0 ? 'var(--sage)' : 'var(--rose)'}">
                            ${txnsSign}$${fmt(Math.abs(txnsSince))}
                        </div>
                    </div>
                    <div class="planning-strip-item">
                        <div class="psi-label">= current balance</div>
                        <div class="psi-val" style="color:var(--text)">$${fmt(currentBalance)}</div>
                    </div>` : ''}
                    <div class="planning-strip-item">
                        <div class="psi-label">+ expected income</div>
                        <div class="psi-val" style="color:var(--text2)">$${fmt(m.summary.expected_income)}</div>
                    </div>
                    <div class="planning-strip-item">
                        <div class="psi-label">− rollovers</div>
                        <div class="psi-val" style="color:var(--rose)">$${fmt(rollover)}</div>
                    </div>
                    <div class="planning-strip-item">
                        <div class="psi-label">− new allocations</div>
                        <div class="psi-val" style="color:var(--rose)">$${fmt(m.summary.new_funding)}</div>
                    </div>
                </div>
            </div>`;

        // Main triage grid: Unallocated Surplus is the big number
        const unallocated = m.summary.expected_income - m.summary.new_funding;
        const uClass = unallocated >= 0 ? 'cv-pos' : 'cv-neg';
        const uSign = unallocated < 0 ? '−' : '';
        triageHTML = `
        <div class="triage-card primary">
            <div class="card-label">Unallocated Surplus</div>
            <div class="card-value ${uClass}">${uSign}$${fmt(Math.abs(unallocated))}</div>
            <div class="card-sub">This month's income not yet assigned to an envelope</div>
            <div class="card-breakdown">
                <div class="breakdown-row">
                    <span class="breakdown-label">expected income</span>
                    <span class="breakdown-val" style="color:var(--text2)">$${fmt(m.summary.expected_income)}</span>
                </div>
                <div class="breakdown-row">
                    <span class="breakdown-label">− new allocations (${Math.round((m.summary.new_funding/m.summary.expected_income)*100)}% of $${fmt(m.summary.expected_income)})</span>
                    <span class="breakdown-val" style="color:var(--rose)">$${fmt(m.summary.new_funding)}</span>
                </div>
                ${m.summary.new_funding < 1 ? `<div class="breakdown-row"><span class="breakdown-label" style="color:var(--rose);font-style:italic">no budgets.csv entries for this month</span></div>` : ''}
            </div>
        </div>
        <div class="triage-card card-excess">
            <div class="card-label">Deployed Capital</div>
            <div class="card-value-md" style="color:var(--sage)">$${fmt(m.summary.total_enveloped)}</div>
            <div class="card-sub">Total cash sitting across all envelopes</div>
        </div>
        <div class="triage-card card-holes">
            <div class="card-label">Needs Attention</div>
            <div class="card-value-md" style="color:var(--rose)">$${fmt(m.summary.holes)}</div>
            <div class="card-sub">Combined shortfall across envelopes</div>
        </div>`;
    } else {
        planningStrip.style.display = 'none';
        const netClass = nh >= 0 ? 'cv-pos' : 'cv-neg';
        triageHTML = `
        <div class="triage-card primary">
            <div class="card-label">Operating Margin</div>
            <div class="card-value ${netClass}">$${fmt(nh)}</div>
            <div class="card-sub">Extra cash across all active envelopes</div>
        </div>
        <div class="triage-card card-excess">
            <div class="card-label">Deployed Capital</div>
            <div class="card-value-md" style="color:var(--sage)">$${fmt(m.summary.excess)}</div>
            <div class="card-sub">Positive cash in operational envelopes</div>
        </div>
        <div class="triage-card card-holes">
            <div class="card-label">Needs Attention</div>
            <div class="card-value-md" style="color:var(--rose)">$${fmt(m.summary.holes)}</div>
            <div class="card-sub">Combined shortfall across envelopes</div>
        </div>`;
    }

    document.getElementById('triageHeader').innerHTML = triageHTML;

    document.getElementById('totalLine').innerHTML =
        `Total enveloped cash &nbsp;·&nbsp; <span>$${fmt(m.summary.total_enveloped)}</span>`;

    // Uncategorized spend notice
    const uncatLine = document.getElementById('uncatLine');
    const uncatDetail = document.getElementById('uncatDetail');
    const us = m.summary.uncategorized_spend;
    const uc = m.summary.uncategorized_cats;
    if (us > 0.01 && !m.is_future) {
        const catCount = Object.keys(uc).length;
        uncatLine.style.display = 'block';
        uncatLine.innerHTML = `&#9432; <span>$${fmt(us)}</span> in ${catCount} untracked categor${catCount === 1 ? 'y' : 'ies'} this month &nbsp;&#8250;`;
        uncatDetail.innerHTML = Object.entries(uc).map(([k, v]) =>
            `<div class="uncat-row"><span class="uncat-cat">${k || '(no category)'}</span><span>$${fmt(v)}</span></div>`
        ).join('');
    } else {
        uncatLine.style.display = 'none';
        uncatDetail.style.display = 'none';
    }

    // Flatten all cats, optionally sort underfunded first
    let allCats = [];
    for (const group in m.categories) {
        m.categories[group].forEach(cat => allCats.push({ ...cat, group }));
    }
    if (sortUnderfunded) {
        allCats.sort((a, b) => {
            const aAvail = a.available + (whatifMode ? (whatifDeltas[a.name] || 0) : 0);
            const bAvail = b.available + (whatifMode ? (whatifDeltas[b.name] || 0) : 0);
            if (aAvail < 0 && bAvail >= 0) return -1;
            if (bAvail < 0 && aAvail >= 0) return 1;
            if (aAvail < 0 && bAvail < 0) return aAvail - bAvail;
            return 0;
        });
    }

    const cats = [];
    let rowIdx = 0;
    let lastGroup = null;
    let html = `<table>
        <thead><tr>
            <th>Envelope</th>
            <th class="th-spark">Trend</th>
            <th>Spent</th>
            <th>Budgeted</th>
            <th>Available</th>
        </tr></thead><tbody>`;

    allCats.forEach((cat, idx) => {
        cats.push(cat.name);
        if (!cat.name.toLowerCase().includes(filter)) return;

        const lId = 'l-' + (cat.group + '-' + idx).replace(/[^a-zA-Z0-9]/g, '-');

        // Apply what-if delta if active
        const wiDelta = whatifMode ? (whatifDeltas[cat.name] || 0) : 0;
        const displayAvail = cat.available + wiDelta;

        const pillClass = displayAvail < -0.01
            ? 'pill-neg'
            : displayAvail > 100
                ? 'pill-green'
                : displayAvail > 0.01
                    ? 'pill-pos'
                    : 'pill-zero';

        const wiIndicator = (whatifMode && Math.abs(wiDelta) > 0.01)
            ? `<span class="sub-ind" style="color:var(--green-dim)">&#8982; ${wiDelta > 0 ? '+' : '−'}$${fmt(wiDelta)}</span>`
            : '';

        const spark = buildSparkline(cat.name);

        // MoM delta
        let deltaHtml = '';
        if (cat.prev_available !== null && cat.prev_available !== undefined) {
            const d = displayAvail - cat.prev_available;
            if (Math.abs(d) > 0.01) {
                const dClass = d > 0 ? 'ind-val-pos' : 'ind-val-neg';
                const dSign = d > 0 ? '+' : '−';
                deltaHtml = `<span class="sub-ind"><span class="ind-label">vs last</span><span class="${dClass}">${dSign}$${fmt(d)}</span></span>`;
            }
        }

        // Coverage % for variable envelopes (current month only)
        let coverageHtml = '';
        if (cat.is_variable && !m.is_future && cat.budgeted > 0) {
            const spentPct = Math.round((cat.spent / cat.budgeted) * 100);
            const monthPct = Math.round(prog.pct * 100);
            const fillColor = spentPct > monthPct + 15 ? 'var(--rose)' : spentPct > monthPct ? 'var(--green)' : 'var(--sage)';
            const eomProjected = prog.pct > 0 ? cat.spent / prog.pct : cat.spent;
            const eomDiff = cat.budgeted - eomProjected;
            const eomSign = eomDiff < 0 ? '−' : '';
            coverageHtml = `
                <div class="coverage-bar"><div class="coverage-fill" style="width:${Math.min(100,spentPct)}%;background:${fillColor}"></div></div>
                <div class="coverage-label">${spentPct}% spent · ${monthPct}% of month</div>
                <div class="coverage-label" style="color:var(--muted)">EOM ~${eomSign}$${fmt(Math.abs(eomDiff))}</div>`;
        }

        // Recurring badge
        const recurBadge = cat.is_recurring
            ? `<span style="font-size:8px;color:var(--muted);margin-left:6px;letter-spacing:1px">↻</span>`
            : '';

        const subRow = (deltaHtml || wiIndicator || coverageHtml)
            ? `<div class="sub-indicators">${deltaHtml}${wiIndicator}</div>${coverageHtml}`
            : '';

        const ledgerItems = cat.ledger
            .slice().sort((a, b) => new Date(b.date) - new Date(a.date))
            .map(l => {
                const ac = l.amt >= 0 ? 'amt-pos' : 'amt-neg';
                return `<div class="ledger-item">
                    <span>${l.date}</span>
                    <span class="truncate">${l.desc}</span>
                    <span class="truncate">${l.note}</span>
                    <span class="${ac}">${fmtSigned(l.amt)}</span>
                </div>`;
            }).join('');

        // Group header if needed
        if (!sortUnderfunded && cat.group !== lastGroup) {
            html += `<tr class="group-header"><td colspan="5">${cat.group}</td></tr>`;
            lastGroup = cat.group;
        } else if (sortUnderfunded && cat.group !== lastGroup) {
            // In underfunded sort mode, show group inline with name
            lastGroup = cat.group;
        }

        const delay = (rowIdx * 18) + 'ms';
        html += `
            <tr class="cat-row" style="animation-delay:${delay}">
                <td><div class="cat-name" onclick="toggleLedger('${lId}')">${cat.name}${recurBadge}</div></td>
                <td class="td-spark">${spark}</td>
                <td class="amount-muted">$${fmt(cat.spent)}</td>
                <td class="amount-muted">$${fmt(cat.budgeted)}</td>
                <td>
                    <div class="avail-cell">
                        <span class="pill ${pillClass}" onclick="smartMove('${cat.name}',${displayAvail})">${displayAvail < 0 ? '−' : ''}$${fmt(displayAvail)}</span>
                        ${subRow}
                    </div>
                </td>
            </tr>
            <tr id="${lId}" class="ledger-row-wrap">
                <td colspan="5">
                    <div class="ledger-inner">
                        <div class="ledger-header">
                            <span>Date</span><span>Description</span><span>Note</span><span>Amount</span>
                        </div>
                        ${ledgerItems || '<div style="padding:10px 0;font-size:11px;color:var(--muted)">No transactions this month.</div>'}
                    </div>
                </td>
            </tr>`;
        rowIdx++;
    });

    document.getElementById('tableBody').innerHTML = html + '</tbody></table>';

    if (!catsPopulated) {
        const f = document.getElementById('fromCat');
        const t = document.getElementById('toCat');
        cats.slice().sort().forEach(c => { f.add(new Option(c, c)); t.add(new Option(c, c)); });
        catsPopulated = true;
    }
}

function toggleLedger(id) {
    const el = document.getElementById(id);
    el.style.display = el.style.display === 'table-row' ? 'none' : 'table-row';
}

function smartMove(name, amt) {
    if (amt < 0) {
        document.getElementById('toCat').value = name;
        document.getElementById('amt').value = Math.abs(amt).toFixed(2);
    } else {
        document.getElementById('fromCat').value = name;
        document.getElementById('amt').value = amt.toFixed(2);
    }
}

function changeMonth(dir) {
    const i = DB.months.indexOf(curMonth);
    if (i + dir >= 0 && i + dir < DB.months.length) {
        curMonth = DB.months[i + dir];
        render();
    }
}

// ── Sort toggle ──
function toggleSort() {
    sortUnderfunded = !sortUnderfunded;
    document.getElementById('underfundedBtn').classList.toggle('active', sortUnderfunded);
    render();
}

// ── What-if mode ──
function toggleWhatif() {
    whatifMode = !whatifMode;
    if (!whatifMode) whatifDeltas = {};
    document.getElementById('whatifBtn').classList.toggle('active', whatifMode);
    document.body.classList.toggle('whatif-active', whatifMode);
    render();
}

function applyWhatifDeltas() {
    // Recompute deltas from current queue for current month
    whatifDeltas = {};
    if (!whatifMode) return;
    transferQueue.forEach(r => {
        if (r.month !== curMonth) return;
        whatifDeltas[r.from] = (whatifDeltas[r.from] || 0) - r.amount;
        whatifDeltas[r.to]   = (whatifDeltas[r.to]   || 0) + r.amount;
    });
}

// ── Smart rebalance ──
function smartRebalance() {
    const m = DB.data_by_month[curMonth];
    const excluded = new Set([...DB.config.health_exclusions, ...DB.config.recurring]);
    
    // Collect underfunded and surplus envelopes
    const underfunded = [];
    const surplus = [];
    for (const group in m.categories) {
        m.categories[group].forEach(cat => {
            if (excluded.has(cat.name)) return;
            if (cat.available < -0.01) underfunded.push({ name: cat.name, need: Math.abs(cat.available) });
            else if (cat.available > 0.01) surplus.push({ name: cat.name, avail: cat.available });
        });
    }
    if (!underfunded.length) { alert('No underfunded envelopes to fix.'); return; }
    if (!surplus.length)     { alert('No surplus envelopes to draw from.'); return; }

    // Sort: most underfunded first, most surplus first
    underfunded.sort((a, b) => b.need - a.need);
    surplus.sort((a, b) => b.avail - a.avail);

    const newMoves = [];
    const surplusLeft = surplus.map(s => ({ ...s }));

    for (const u of underfunded) {
        let remaining = u.need;
        for (const s of surplusLeft) {
            if (s.avail < 0.01 || remaining < 0.01) continue;
            const move = Math.min(s.avail, remaining);
            newMoves.push({ month: curMonth, from: s.name, to: u.name, amount: parseFloat(move.toFixed(2)), note: 'smart rebalance' });
            s.avail -= move;
            remaining -= move;
            if (remaining < 0.01) break;
        }
    }

    if (!newMoves.length) return;
    transferQueue.push(...newMoves);
    if (whatifMode) applyWhatifDeltas();
    renderQueue();
    if (whatifMode) render();

    // Scroll to footer
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
}

function toggleUncatDetail() {
    const d = document.getElementById('uncatDetail');
    d.style.display = d.style.display === 'block' ? 'none' : 'block';
}

// ── Transfer queue ──
function addToQueue() {
    const f = document.getElementById('fromCat').value;
    const t = document.getElementById('toCat').value;
    const a = parseFloat(document.getElementById('amt').value);
    const n = document.getElementById('note').value.trim();
    if (!f || !t || !a || f === t) return;
    transferQueue.push({ month: curMonth, from: f, to: t, amount: a, note: n || 'rebalance' });
    document.getElementById('amt').value = '';
    document.getElementById('note').value = '';
    if (whatifMode) { applyWhatifDeltas(); render(); }
    renderQueue();
}

function removeFromQueue(idx) {
    transferQueue.splice(idx, 1);
    if (whatifMode) { applyWhatifDeltas(); render(); }
    renderQueue();
}

function renderQueue() {
    const list   = document.getElementById('queueList');
    const badge  = document.getElementById('queueCount');
    const copyBtn = document.getElementById('copyAllBtn');
    badge.textContent = transferQueue.length;
    badge.style.display = transferQueue.length ? 'inline' : 'none';
    copyBtn.disabled = transferQueue.length === 0;

    if (transferQueue.length === 0) {
        list.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:8px 0 10px;">Queue is empty — add transfers above.</div>';
    } else {
        list.innerHTML = transferQueue.map((r, i) => `
            <div class="queue-item">
                <button class="queue-item-remove" onclick="removeFromQueue(${i})">&#215;</button>
                <span style="color:var(--muted)">${r.month}</span>
                <span style="color:var(--rose)">${r.from}</span>
                <span style="color:var(--muted)">&#8594;</span>
                <span style="color:var(--sage)">${r.to}</span>
                <span style="color:var(--green)">$${r.amount.toFixed(2)}</span>
                <span style="color:var(--muted);font-size:10px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.note}</span>
            </div>`).join('');
        if (!queueOpen) {
            queueOpen = true;
            list.style.display = 'block';
            document.getElementById('queueHint').textContent = '&#9660; hide queue';
        }
    }
}

function toggleQueue() {
    queueOpen = !queueOpen;
    document.getElementById('queueList').style.display = queueOpen ? 'block' : 'none';
    document.getElementById('queueHint').innerHTML = queueOpen ? '&#9660; hide queue' : '&#9650; show queue';
}

function copyQueue() {
    if (!transferQueue.length) return;
    const csv = transferQueue
        .map(r => `${r.month},"${r.from}","${r.to}",${r.amount.toFixed(2)},"${r.note}"`)
        .join('\\n');
    navigator.clipboard.writeText(csv).then(() => {
        const btn = document.getElementById('copyAllBtn');
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = 'Copy All'; }, 1600);
    });
}

// ── Export ──
function openExport() {
    const m = DB.data_by_month[curMonth];
    document.getElementById('exportMonthLabel').textContent = curMonth;
    let csv = 'Envelope,Spent,Budgeted,Available\\n';
    for (const group in m.categories) {
        csv += `"-- ${group} --",,,\\n`;
        m.categories[group].forEach(cat => {
            csv += `"${cat.name}",${cat.spent.toFixed(2)},${cat.budgeted.toFixed(2)},${cat.available.toFixed(2)}\\n`;
        });
    }
    csv += `\\nSummary,,\\n`;
    csv += `Operating Margin,,${m.summary.net_health.toFixed(2)}\\n`;
    csv += `Deployed Capital,,${m.summary.excess.toFixed(2)}\\n`;
    csv += `Shortfall,,${m.summary.holes.toFixed(2)}\\n`;
    csv += `Total Enveloped,,${m.summary.total_enveloped.toFixed(2)}\\n`;
    document.getElementById('exportText').value = csv;
    document.getElementById('exportModal').classList.add('open');
}

function closeExport() {
    document.getElementById('exportModal').classList.remove('open');
}

function copyExport() {
    navigator.clipboard.writeText(document.getElementById('exportText').value).then(() => {
        const btn = document.getElementById('modalCopyBtn');
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = 'Copy to Clipboard'; }, 1500);
    });
}

document.getElementById('exportModal').addEventListener('click', function(e) {
    if (e.target === this) closeExport();
});

render();
renderQueue();
</script>
</body>
</html>
""".replace("__DATA__", json.dumps(process_data()))

with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
    f.write(HTML_TEMPLATE)
print(f"Generated {OUTPUT_HTML}")