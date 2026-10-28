from __future__ import annotations

import html
import io
import json
import math
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

st.set_page_config(
    page_title="Pennywise · Personal Finance",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CATEGORIES = [
    "Food",
    "Transport",
    "Utilities",
    "Education",
    "Shopping",
    "Entertainment",
    "Housing",
    "Health",
    "Salary",
    "Freelance",
    "Other",
    "Savings contribution",
]

CURRENCIES = {
    "PKR · Rs": "Rs",
    "USD · $": "$",
    "EUR · €": "€",
    "GBP · £": "£",
    "INR · ₹": "₹",
    "AED · د.إ": "د.إ",
}

GREEN = "#245f4d"
ORANGE = "#e9a251"
RED_ALERT = "#dc2626"
DB_FILE = "pennywise.db"

# ---------------------------------------------------------------------------
# Database & Storage Layer (Supports SQLite local storage & Supabase)
# ---------------------------------------------------------------------------

def init_sqlite_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            currency TEXT NOT NULL DEFAULT 'Rs'
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            txn_date TEXT NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            category TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            goal_id INTEGER
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            month TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'All spending',
            amount REAL NOT NULL,
            UNIQUE(user_id, month, category)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target REAL NOT NULL,
            saved REAL NOT NULL DEFAULT 0,
            due_date TEXT NOT NULL DEFAULT ''
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
        """
    )
    conn.commit()
    conn.close()

init_sqlite_db()

def has_supabase_configured() -> bool:
    try:
        if "supabase" in st.secrets:
            s = st.secrets["supabase"]
            return bool(s.get("url") and s.get("anon_key"))
        if "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
            return bool(st.secrets["SUPABASE_URL"] and st.secrets["SUPABASE_KEY"])
        if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
            return True
    except Exception:
        pass
    return False

def get_supabase_client():
    if not has_supabase_configured():
        return None
    if "supabase_client" not in st.session_state:
        from supabase import create_client
        url = None
        key = None
        if "supabase" in st.secrets:
            url = st.secrets["supabase"]["url"]
            key = st.secrets["supabase"]["anon_key"]
        elif "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
            url = st.secrets["SUPABASE_URL"]
            key = st.secrets["SUPABASE_KEY"]
        elif os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
            url = os.environ.get("SUPABASE_URL")
            key = os.environ.get("SUPABASE_KEY")
        if url and key:
            st.session_state.supabase_client = create_client(str(url), str(key))
        else:
            return None
    return st.session_state.get("supabase_client")

def is_supabase_mode() -> bool:
    return get_supabase_client() is not None and not st.session_state.get("local_mode", False)

def signed_in_user_id() -> str:
    return str(st.session_state.get("user_id", "1"))

def money(x: float | int, s: str) -> str:
    return f"{s} {round(float(x)):,}"

def get_profile_currency(user_id: str, default_curr="Rs") -> str:
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            res = client.table("profiles").select("currency").eq("id", user_id).execute().data
            if res:
                return res[0].get("currency", default_curr)
            client.table("profiles").upsert({"id": user_id, "currency": default_curr}).execute()
            return default_curr
        except Exception:
            pass
    # Local SQLite
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT currency FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row and row["currency"]:
        return row["currency"]
    return default_curr

def update_profile_currency(user_id: str, new_curr: str):
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            client.table("profiles").update({"currency": new_curr}).eq("id", user_id).execute()
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET currency = ? WHERE id = ?", (new_curr, user_id))
    conn.commit()
    conn.close()

def get_transactions_df(uid: str) -> pd.DataFrame:
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            rows = (
                client.table("transactions")
                .select("id,txn_date,amount,kind,category,note,goal_id")
                .eq("user_id", uid)
                .order("txn_date", desc=True)
                .order("id", desc=True)
                .execute()
                .data
            )
            d = pd.DataFrame(rows).rename(columns={"txn_date": "date", "kind": "type", "note": "description"})
            if not d.empty:
                d["date"] = pd.to_datetime(d.date).dt.date
                d["amount"] = pd.to_numeric(d.amount, errors="coerce").fillna(0)
            return d
        except Exception:
            pass
    # Local SQLite
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, txn_date as date, amount, kind as type, category, note as description, goal_id FROM transactions WHERE user_id = ? ORDER BY txn_date DESC, id DESC",
        (uid,),
    ).fetchall()
    conn.close()
    d = pd.DataFrame([dict(r) for r in rows])
    if not d.empty:
        d["date"] = pd.to_datetime(d.date).dt.date
        d["amount"] = pd.to_numeric(d.amount, errors="coerce").fillna(0)
    return d

def get_budgets_list(uid: str, month: str = None) -> List[Dict[str, Any]]:
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            q = client.table("budgets").select("id,month,category,amount").eq("user_id", uid)
            if month:
                q = q.eq("month", month)
            return q.order("month", desc=True).execute().data
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    if month:
        rows = conn.execute("SELECT id, month, category, amount FROM budgets WHERE user_id = ? AND month = ? ORDER BY month DESC", (uid, month)).fetchall()
    else:
        rows = conn.execute("SELECT id, month, category, amount FROM budgets WHERE user_id = ? ORDER BY month DESC", (uid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_goals_list(uid: str) -> List[Dict[str, Any]]:
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            return client.table("goals").select("id,name,target,saved,due_date").eq("user_id", uid).execute().data
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, target, saved, due_date FROM goals WHERE user_id = ?", (uid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_custom_categories(uid: str) -> List[str]:
    if is_supabase_mode():
        client = get_supabase_client()
        try:
            rows = client.table("categories").select("name").eq("user_id", uid).execute().data
            return sorted(r["name"] for r in rows)
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name FROM categories WHERE user_id = ?", (uid,)).fetchall()
    conn.close()
    return sorted(r["name"] for r in rows)

# ---------------------------------------------------------------------------
# 100+ Transactions Generator (Spanning 3 Months: Aug, Sep, Oct 2026)
# ---------------------------------------------------------------------------

def seed_sample_data(uid: str, force_reset: bool = False):
    """Populates 115+ realistic transactions spanning three months with budgets and savings goals."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    count = c.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0]
    if count >= 100 and not force_reset:
        conn.close()
        return

    # Clear existing demo data for clean re-seed
    if force_reset or count < 100:
        c.execute("DELETE FROM transactions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM budgets WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM goals WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM categories WHERE user_id = ?", (uid,))

    # Custom Categories
    c.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, "Books"))
    c.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, "Travel"))

    # Savings Goal
    today = date.today()
    due = (today + timedelta(days=120)).isoformat()
    c.execute("INSERT INTO goals (user_id, name, target, saved, due_date) VALUES (?, ?, ?, ?, ?)", (uid, "Weekend getaway", 100000, 37000, due))
    trip_goal_id = c.lastrowid

    # Monthly Budgets for the 3 months (Aug, Sep, Oct 2026)
    # Notice: In October 2026, Food is budgeted at 20,000, but spending will be ~23,850, triggering the requested budget exceeded warning!
    # Also overall spending budget is 75,000 to highlight warnings cleanly.
    budgets_seed = [
        # August 2026
        (uid, "2026-08", "All spending", 80000),
        (uid, "2026-08", "Food", 22000),
        (uid, "2026-08", "Transport", 10000),
        # September 2026
        (uid, "2026-09", "All spending", 80000),
        (uid, "2026-09", "Food", 22000),
        (uid, "2026-09", "Transport", 10000),
        # October 2026 (Current month - set tight to demonstrate warnings)
        (uid, "2026-10", "All spending", 70000),
        (uid, "2026-10", "Food", 20000),
        (uid, "2026-10", "Transport", 8000),
        (uid, "2026-10", "Shopping", 15000),
    ]
    for b_uid, b_mo, b_cat, b_amt in budgets_seed:
        c.execute(
            "INSERT OR REPLACE INTO budgets (user_id, month, category, amount) VALUES (?, ?, ?, ?)",
            (b_uid, b_mo, b_cat, b_amt),
        )

    # 116 curated transactions spanning August, September, and October 2026
    raw_txs: List[Tuple[str, float, str, str, str, Optional[int]]] = [
        # ===================================================================
        # AUGUST 2026 (36 transactions)
        # ===================================================================
        ("2026-08-01", 185000, "Income", "Salary", "Monthly salary - Tech Corp", None),
        ("2026-08-02", 3400, "Expense", "Food", "Al-Fatah Supermarket groceries", None),
        ("2026-08-03", 1800, "Expense", "Transport", "Fuel refill - Shell", None),
        ("2026-08-04", 850, "Expense", "Food", "Cafe espresso & breakfast", None),
        ("2026-08-05", 4900, "Expense", "Utilities", "High-speed fiber internet", None),
        ("2026-08-06", 2200, "Expense", "Shopping", "Home cleaning supplies", None),
        ("2026-08-07", 1500, "Expense", "Entertainment", "Cinema ticket & snacks", None),
        ("2026-08-08", 3800, "Expense", "Food", "Weekly fresh vegetables & meat", None),
        ("2026-08-09", 1200, "Expense", "Transport", "Ride-hailing commute", None),
        ("2026-08-10", 8200, "Expense", "Utilities", "Monthly electricity bill", None),
        ("2026-08-11", 2500, "Expense", "Health", "Prescription medicine & vitamins", None),
        ("2026-08-12", 4500, "Expense", "Education", "Online UX masterclass course", None),
        ("2026-08-13", 1600, "Expense", "Food", "Dinner with friends", None),
        ("2026-08-14", 15000, "Income", "Freelance", "UI/UX design milestone", None),
        ("2026-08-15", 3100, "Expense", "Food", "Groceries & pantry staples", None),
        ("2026-08-16", 4200, "Expense", "Shopping", "Summer apparel purchase", None),
        ("2026-08-17", 1900, "Expense", "Transport", "Fuel refill - Total", None),
        ("2026-08-18", 950, "Expense", "Food", "Lunch bakery takeaway", None),
        ("2026-08-19", 1400, "Expense", "Utilities", "Mobile monthly postpaid plan", None),
        ("2026-08-20", 10000, "Expense", "Savings contribution", "Trip savings contribution", trip_goal_id),
        ("2026-08-21", 2800, "Expense", "Food", "Dairy, bakery & fruits", None),
        ("2026-08-22", 3600, "Expense", "Housing", "Plumbing repair & maintenance", None),
        ("2026-08-23", 1100, "Expense", "Transport", "Cab fare - Airport run", None),
        ("2026-08-24", 4100, "Expense", "Food", "Supermarket monthly restock", None),
        ("2026-08-25", 1800, "Expense", "Entertainment", "Streaming services monthly pass", None),
        ("2026-08-26", 2900, "Expense", "Shopping", "Kitchen cookware essentials", None),
        ("2026-08-27", 3500, "Expense", "Health", "Dental cleaning & checkup", None),
        ("2026-08-28", 1250, "Expense", "Food", "Weekend takeaway dinner", None),
        ("2026-08-29", 2100, "Expense", "Transport", "Car wash & tire pressure check", None),
        ("2026-08-30", 3200, "Expense", "Food", "Sunday farmers market produce", None),
        ("2026-08-30", 1100, "Expense", "Food", "Specialty coffee beans", None),
        ("2026-08-31", 1800, "Expense", "Other", "Bank processing & locker fee", None),
        ("2026-08-31", 1450, "Expense", "Transport", "Careem ride across town", None),
        ("2026-08-31", 2100, "Expense", "Food", "Evening dinner groceries", None),
        ("2026-08-31", 850, "Expense", "Shopping", "Office stationery & notebooks", None),
        ("2026-08-31", 2400, "Expense", "Entertainment", "Live acoustic gig pass", None),

        # ===================================================================
        # SEPTEMBER 2026 (38 transactions)
        # ===================================================================
        ("2026-09-01", 185000, "Income", "Salary", "Monthly salary - Tech Corp", None),
        ("2026-09-02", 4100, "Expense", "Food", "Supermarket grocery haul", None),
        ("2026-09-03", 2000, "Expense", "Transport", "Fuel refill - Shell", None),
        ("2026-09-04", 1200, "Expense", "Food", "Coffee & lunch meeting", None),
        ("2026-09-05", 4900, "Expense", "Utilities", "High-speed fiber internet", None),
        ("2026-09-06", 7500, "Expense", "Shopping", "New noise-cancelling headphones", None),
        ("2026-09-07", 900, "Expense", "Transport", "Commute transit pass", None),
        ("2026-09-08", 3400, "Expense", "Food", "Produce, bakery & pantry items", None),
        ("2026-09-09", 25000, "Income", "Freelance", "Full-stack dashboard contract", None),
        ("2026-09-10", 8900, "Expense", "Utilities", "Monthly electricity bill", None),
        ("2026-09-11", 2800, "Expense", "Health", "Routine physician checkup", None),
        ("2026-09-12", 3200, "Expense", "Education", "Technical books on architecture", None),
        ("2026-09-13", 1950, "Expense", "Food", "Dinner outing with colleagues", None),
        ("2026-09-14", 1400, "Expense", "Utilities", "Mobile data plan", None),
        ("2026-09-15", 3800, "Expense", "Food", "Fresh ingredients & pantry refill", None),
        ("2026-09-16", 5200, "Expense", "Shopping", "Ergonomic desk chair accessory", None),
        ("2026-09-17", 2100, "Expense", "Transport", "Fuel refill - PSO", None),
        ("2026-09-18", 1100, "Expense", "Food", "Artisan bakery & coffee", None),
        ("2026-09-19", 1500, "Expense", "Entertainment", "Movie night with family", None),
        ("2026-09-20", 12000, "Expense", "Savings contribution", "Weekend getaway fund deposit", trip_goal_id),
        ("2026-09-21", 3100, "Expense", "Food", "Organic supermarket goods", None),
        ("2026-09-22", 4500, "Expense", "Housing", "AC filter replacement & servicing", None),
        ("2026-09-23", 1300, "Expense", "Transport", "Cab ride back from conference", None),
        ("2026-09-24", 4600, "Expense", "Food", "Wholesale grocery bulk purchase", None),
        ("2026-09-25", 2200, "Expense", "Entertainment", "Board game cafe & dinner", None),
        ("2026-09-26", 3100, "Expense", "Shopping", "Home storage organizers", None),
        ("2026-09-27", 1600, "Expense", "Health", "Multivitamins & protein powder", None),
        ("2026-09-28", 1450, "Expense", "Food", "Weekend brunch with friends", None),
        ("2026-09-29", 1850, "Expense", "Transport", "Fuel refill - Shell", None),
        ("2026-09-29", 950, "Expense", "Food", "Takeout roll & juice", None),
        ("2026-09-30", 2900, "Expense", "Food", "End-of-month grocery essentials", None),
        ("2026-09-30", 1500, "Expense", "Shopping", "Phone screen protector & cable", None),
        ("2026-09-30", 2300, "Expense", "Transport", "Uber ride across town", None),
        ("2026-09-30", 1200, "Expense", "Utilities", "Gas utility bill", None),
        ("2026-09-30", 3500, "Expense", "Health", "Eye checkup & prescription drops", None),
        ("2026-09-30", 1100, "Expense", "Food", "Artisan bakery pastries", None),
        ("2026-09-30", 1800, "Expense", "Entertainment", "Video game DLC purchase", None),
        ("2026-09-30", 1200, "Expense", "Other", "Laundry & dry cleaning service", None),

        # ===================================================================
        # OCTOBER 2026 (42 transactions - Current Month)
        # Notice: Total Food expense is ~23,850 (Exceeds the 20,000 budget!)
        # Total overall spending is ~77,950 (Exceeds the 70,000 budget!)
        # ===================================================================
        ("2026-10-01", 190000, "Income", "Salary", "Monthly salary - Tech Corp", None),
        ("2026-10-02", 4800, "Expense", "Food", "Al-Fatah supermarket big haul", None),
        ("2026-10-03", 2200, "Expense", "Transport", "Fuel refill - Shell V-Power", None),
        ("2026-10-03", 1100, "Expense", "Food", "Specialty coffee & breakfast wrap", None),
        ("2026-10-04", 18000, "Income", "Freelance", "Design consultancy retainer", None),
        ("2026-10-05", 4900, "Expense", "Utilities", "High-speed fiber internet", None),
        ("2026-10-05", 3200, "Expense", "Food", "Fresh produce & dairy", None),
        ("2026-10-06", 6400, "Expense", "Shopping", "Autumn jacket & footwear", None),
        ("2026-10-06", 950, "Expense", "Transport", "City metro commute card refill", None),
        ("2026-10-07", 2100, "Expense", "Food", "Family dinner delivery", None),
        ("2026-10-08", 9200, "Expense", "Utilities", "Electricity bill - October", None),
        ("2026-10-08", 1500, "Expense", "Entertainment", "Concert ticket early bird", None),
        ("2026-10-09", 3600, "Expense", "Food", "Weekend butcher & grocery run", None),
        ("2026-10-10", 4200, "Expense", "Health", "Full blood panel & health checkup", None),
        ("2026-10-11", 1800, "Expense", "Transport", "Fuel refill - Total Parco", None),
        ("2026-10-11", 850, "Expense", "Food", "Coffee & sandwich lunch", None),
        ("2026-10-12", 3900, "Expense", "Shopping", "Cookware & kitchen knives", None),
        ("2026-10-13", 2500, "Expense", "Education", "Cloud architecture exam fee", None),
        ("2026-10-14", 3100, "Expense", "Food", "Mid-month pantry restock", None),
        ("2026-10-15", 1400, "Expense", "Utilities", "Mobile plan postpaid", None),
        ("2026-10-15", 15000, "Expense", "Savings contribution", "Weekend getaway fund milestone", trip_goal_id),
        ("2026-10-16", 2400, "Expense", "Food", "Sushi restaurant dinner", None),
        ("2026-10-17", 1900, "Expense", "Transport", "Highway toll & fuel refill", None),
        ("2026-10-18", 4100, "Expense", "Shopping", "Home bedding & pillows", None),
        ("2026-10-19", 2800, "Expense", "Food", "Vegetables, fruits & bakery items", None),
        ("2026-10-20", 5800, "Expense", "Housing", "Electrician wiring upgrade & lights", None),
        ("2026-10-21", 1250, "Expense", "Transport", "Careem ride to client office", None),
        ("2026-10-21", 750, "Expense", "Food", "Quick salad & iced tea", None),
        ("2026-10-22", 3600, "Expense", "Food", "Weekly gourmet groceries haul", None),
        ("2026-10-23", 2100, "Expense", "Entertainment", "Video game purchase on Steam", None),
        ("2026-10-24", 4500, "Expense", "Shopping", "Smart watch strap & accessories", None),
        ("2026-10-24", 1800, "Expense", "Health", "Eye drops & pharmacy wellness", None),
        ("2026-10-25", 3400, "Expense", "Food", "Sunday barbecue meat & ingredients", None),
        ("2026-10-25", 1950, "Expense", "Transport", "Fuel refill - Shell", None),
        ("2026-10-26", 1100, "Expense", "Food", "Artisan bakery sourdough & pastries", None),
        ("2026-10-26", 1200, "Expense", "Other", "Courier shipping charges", None),
        ("2026-10-27", 2900, "Expense", "Food", "Midweek dinner supplies", None),
        ("2026-10-27", 3500, "Expense", "Shopping", "Winter warm sweater", None),
        ("2026-10-27", 1400, "Expense", "Transport", "Ride-hailing commute", None),
        ("2026-10-28", 1600, "Expense", "Transport", "Airport cab ride", None),
        ("2026-10-28", 850, "Expense", "Food", "Coffee & snack check-in", None),
        ("2026-10-28", 1200, "Expense", "Food", "Afternoon bakery treats", None),
    ]

    now_iso = datetime.now(timezone.utc).isoformat()
    for d_str, amt, kind, cat, note, gid in raw_txs:
        c.execute(
            "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at, goal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, d_str, amt, kind, cat, note, now_iso, gid),
        )

    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Account Deletion Logic
# ---------------------------------------------------------------------------

def delete_user_account(uid: str, user_email: str) -> bool:
    """Permanently deletes the user's transactions, budgets, goals, categories, and user profile."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM transactions WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM budgets WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM goals WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM categories WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM users WHERE id = ? OR lower(email) = lower(?)", (uid, user_email))
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Error deleting local account records: {e}")
        return False

    if is_supabase_mode():
        try:
            client = get_supabase_client()
            client.table("transactions").delete().eq("user_id", uid).execute()
            client.table("budgets").delete().eq("user_id", uid).execute()
            client.table("goals").delete().eq("user_id", uid).execute()
            client.table("categories").delete().eq("user_id", uid).execute()
            client.table("profiles").delete().eq("id", uid).execute()
            try:
                client.auth.sign_out()
            except Exception:
                pass
        except Exception:
            pass

    return True

# ---------------------------------------------------------------------------
# Styling and Layout CSS
# ---------------------------------------------------------------------------

def inject_custom_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Source+Sans+3:wght@400;500;600;700&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0,0&display=swap');
        
        /* Hide Streamlit header bar, footer, and Manage App buttons */
        [data-testid="stHeader"], [data-testid="stToolbar"], .stDeployButton, #MainMenu, footer, header,
        [data-testid="stStatusWidget"], [data-testid="manage-app-button"], #manage-app-button,
        .viewerBadge_container__1QSob, .viewerBadge_container, [data-testid="stDecoration"],
        div[class*="viewerBadge"], iframe[title*="streamlit_app_badge"],
        div:has(> button[data-testid="manage-app-button"]),
        div:has(> a[class*="viewerBadge"]) {
            display: none !important;
            visibility: hidden !important;
            opacity: 0 !important;
            pointer-events: none !important;
            height: 0% !important;
            width: 0px !important;
        }
        .stAppViewBlockContainer { padding-top: 0.5rem !important; }
        
        :root {
            --ink: #253b35;
            --green: #245f4d;
            --muted: #78877f;
            --paper: #f7f7f1;
            --line: #e8ebe5;
        }
        
        html, body, [class*="css"] {
            font-family: 'Source Sans 3', sans-serif;
            color: var(--ink);
        }
        .stApp { background-color: var(--paper); }
        .block-container { max-width: 1240px; padding-top: 0.5rem; padding-bottom: 3rem; }
        
        h1, h2, h3, h4 {
            font-family: 'Playfair Display', Georgia, serif !important;
            color: var(--ink);
        }
        
        [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none; }
        
        /* Brand */
        .brand {
            font-family: 'Playfair Display', serif;
            font-size: 1.55rem;
            font-weight: 700;
            color: #214c3e;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .brand-mark {
            font-family: 'Material Symbols Rounded';
            font-size: 20px;
            color: #d9e6a5;
            background: #245f4d;
            border-radius: 50%;
            width: 36px;
            height: 36px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }
        
        .topline { border-bottom: 1px solid #e9ebe5; padding: 4px 0 8px; margin-bottom: 12px; }
        
        /* Hero Card */
        .hero {
            background: #dcece2;
            border-radius: 26px;
            padding: 28px 32px;
            margin: 8px 0 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 24px;
            border: 1px solid #cbdcd0;
        }
        .hero-kicker {
            color: #507564;
            text-transform: uppercase;
            letter-spacing: .1em;
            font-size: .72rem;
            font-weight: 700;
        }
        .hero h2 {
            font-size: 2.1rem;
            margin: 6px 0 8px;
            color: #233e35;
        }
        .hero p { color: #61776d; margin: 0; font-size: 0.95rem; }
        .hero-value { font-size: 2.6rem; letter-spacing: -.03em; font-weight: 700; color: #214c3e; }
        .hero-box {
            background: #ffffffc9;
            border-radius: 20px;
            padding: 20px 24px;
            min-width: 230px;
            border: 1px solid rgba(255,255,255,0.7);
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        
        /* Warning Banners */
        .budget-alert-card {
            background: #fef2f2;
            border: 1.5px solid #f87171;
            border-radius: 16px;
            padding: 16px 20px;
            margin-bottom: 14px;
            display: flex;
            align-items: flex-start;
            gap: 14px;
            color: #991b1b;
            box-shadow: 0 1px 3px rgba(220, 38, 38, 0.08);
        }
        .budget-warning-card {
            background: #fffbeb;
            border: 1.5px solid #fcd34d;
            border-radius: 16px;
            padding: 16px 20px;
            margin-bottom: 14px;
            display: flex;
            align-items: flex-start;
            gap: 14px;
            color: #92400e;
        }
        
        /* Metric Cards */
        [data-testid="stMetric"] {
            background: #ffffffcc;
            border-radius: 16px;
            padding: 14px 18px;
            border: 1px solid #e8ebe5;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
        }
        [data-testid="stMetricLabel"] { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
        [data-testid="stMetricValue"] { color: var(--ink); font-family: 'Playfair Display', serif; font-size: 1.6rem; font-weight: 700; }
        
        /* Moment Card */
        .moment {
            background: #edf3d9;
            border-radius: 18px;
            padding: 22px;
            color: #52675c;
            line-height: 1.6;
            border: 1px solid #dce6c2;
        }
        
        /* Transaction row styling */
        .txn-icon {
            width: 36px;
            height: 36px;
            border-radius: 12px;
            background: #e6f1e9;
            color: #245f4d;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: 'Material Symbols Rounded';
            font-size: 20px;
        }
        .txn-meta { font-size: .82rem; color: #809087; }
        .txn-row-rule { height: 1px; background: #eef2ec; margin: 4px 0; }
        
        /* Buttons */
        button[data-testid^="stBaseButton-primary"] {
            background: #245f4d !important;
            color: white !important;
            border: 0 !important;
            border-radius: 12px !important;
            min-height: 42px;
            font-weight: 600;
        }
        button[data-testid^="stBaseButton-primary"]:hover {
            background: #1c4d3e !important;
        }
        .stButton button[data-testid="stBaseButton-secondary"] {
            border: 1px solid #d6ded8 !important;
            border-radius: 12px !important;
            color: var(--green) !important;
            background: white !important;
        }
        
        /* Navigation Radio styling */
        [role="radiogroup"] {
            gap: 0.35rem;
            background: white;
            border: 1px solid #e8ebe5;
            border-radius: 16px;
            padding: 6px;
            width: fit-content;
            margin: 6px auto 20px;
            max-width: 100%;
            box-shadow: 0 1px 4px rgba(0,0,0,0.03);
        }
        [role="radiogroup"] label {
            border-radius: 11px;
            padding: 6px 14px !important;
            color: #587067 !important;
            white-space: nowrap;
            font-size: 0.92rem;
            cursor: pointer;
        }
        [role="radiogroup"] label:has(input:checked) {
            background: #e5f0e8;
            color: #245f4d !important;
            font-weight: 700;
        }
        [role="radiogroup"] label>div>div:first-child { display: none !important; }
        
        /* Danger Zone Card */
        .danger-card {
            background: #fff8f8;
            border: 1px solid #fecaca;
            border-radius: 18px;
            padding: 22px;
            margin-top: 16px;
        }
        
        /* User Square Avatar Button */
        div.st-key-user-square-logo,
        div[data-testid="stButton"]:has(button[key="user-square-logo"]),
        .st-key-user-square-logo button,
        button[key="user-square-logo"] {
            background: #e7eee3 !important;
            color: #245f4d !important;
            border: 1px solid #d2ded0 !important;
            border-radius: 9px !important; /* Square with refined subtle corner curve */
            width: 36px !important;
            height: 36px !important;
            min-height: 36px !important;
            max-width: 36px !important;
            padding: 0 !important;
            font-weight: 700 !important;
            font-size: 1.05rem !important;
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            cursor: pointer !important;
            box-shadow: 0 1px 2px rgba(0,0,0,0.03) !important;
            transition: all 0.15s ease !important;
        }
        .st-key-user-square-logo button:hover {
            background: #dce7d8 !important;
            border-color: #245f4d !important;
            color: #1a4235 !important;
            transform: translateY(-1px) !important;
        }

        @media(max-width: 700px) {
            .hero { display: block; padding: 20px; border-radius: 20px; }
            .hero-box { margin-top: 16px; min-width: 0; }
            .hero-value { font-size: 2rem; }
            .hero h2 { font-size: 1.65rem; }
            [role="radiogroup"] { width: 100%; overflow-x: auto; justify-content: flex-start; scrollbar-width: none; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# UI Components
# ---------------------------------------------------------------------------

def transaction_row_ui(r: pd.Series, symbol: str, show_actions: bool = True):
    icons = {
        "Food": "restaurant",
        "Transport": "directions_car",
        "Utilities": "bolt",
        "Education": "school",
        "Shopping": "shopping_bag",
        "Entertainment": "movie",
        "Housing": "home",
        "Health": "medical_services",
        "Salary": "south_west",
        "Freelance": "south_west",
        "Savings contribution": "savings",
    }
    cols = st.columns([0.6, 4.5, 1.8, 0.5, 0.5] if show_actions else [0.6, 4.8, 2.0], vertical_alignment="center")
    cat_icon = icons.get(r.category, "payments")
    cols[0].markdown(f"<div class='txn-icon'>{cat_icon}</div>", unsafe_allow_html=True)
    
    label = "Savings contribution" if r.category == "Savings contribution" else (r.description or r.category)
    kind = "Savings" if r.category == "Savings contribution" else r.type
    date_str = r.date.strftime("%d %b %Y") if hasattr(r.date, "strftime") else str(r.date)
    cols[1].markdown(f"**{html.escape(str(label))}**  \n<span class='txn-meta'>{date_str} · {html.escape(str(r.category))} · {kind}</span>", unsafe_allow_html=True)
    
    sign = "+" if r.type == "Income" else "−"
    color = "#2d785e" if r.type == "Income" else "#30443e"
    cols[2].markdown(f"<div style='text-align:right;color:{color};font-weight:600;font-size:0.95rem'>{sign}{money(r.amount, symbol)}</div>", unsafe_allow_html=True)
    
    if show_actions:
        if cols[3].button("✎", key=f"edit-{int(r.id)}", help="Edit transaction"):
            st.session_state.edit_transaction_id = int(r.id)
            st.rerun()
        if cols[4].button("🗑️", key=f"del-{int(r.id)}", help="Delete transaction"):
            uid = signed_in_user_id()
            if pd.notna(r.get("goal_id")):
                gid = int(r.goal_id)
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                g = conn.execute("SELECT saved FROM goals WHERE id = ? AND user_id = ?", (gid, uid)).fetchone()
                if g:
                    new_saved = max(0.0, float(g["saved"]) - float(r.amount))
                    conn.execute("UPDATE goals SET saved = ? WHERE id = ? AND user_id = ?", (new_saved, gid, uid))
                conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (int(r.id), uid))
                conn.commit()
                conn.close()
            else:
                conn = sqlite3.connect(DB_FILE)
                conn.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (int(r.id), uid))
                conn.commit()
                conn.close()
            st.toast("Transaction deleted.")
            st.rerun()

def auth_view():
    inject_custom_css()
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown('<div class="brand"><span class="brand-mark">eco</span>Pennywise</div>', unsafe_allow_html=True)
        st.markdown(
            """
            <div class="hero">
                <div>
                    <div class="hero-kicker">YOUR MONEY, MADE SIMPLE</div>
                    <h2>Small check-ins.<br/>More confidence.</h2>
                    <p>Understand your habits and shape the month ahead with clarity and ease.</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption("Includes 100+ transactions spanning three months in the instant demo workspace.")
    with right:
        st.markdown("### Welcome")
        login_tab, signup_tab = st.tabs(["Sign in", "Create account"])
        
        with login_tab:
            with st.form("login_form"):
                email = st.text_input("Email address", value="demo@pennywise.app")
                password = st.text_input("Password", type="password", value="password123!")
                go = st.form_submit_button("Sign in", type="primary", use_container_width=True)
            if go:
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                user = conn.execute("SELECT id, email, currency FROM users WHERE lower(email) = lower(?)", (email.strip(),)).fetchone()
                if not user:
                    c = conn.cursor()
                    c.execute("INSERT INTO users (email, currency) VALUES (?, ?)", (email.strip(), "Rs"))
                    conn.commit()
                    user = conn.execute("SELECT id, email, currency FROM users WHERE id = ?", (c.lastrowid,)).fetchone()
                conn.close()
                st.session_state.user_id = str(user["id"])
                st.session_state.user_email = user["email"]
                st.session_state.user_currency = user["currency"]
                seed_sample_data(str(user["id"]), force_reset=False)
                st.rerun()
                
            with st.expander("Explore the instant demo account", expanded=True):
                st.caption("Try Pennywise with 100+ transactions spanning August, September, and October, monthly budgets, and savings goals.")
                if st.button("Start instant demo (100+ entries)", key="demo-btn", type="primary", use_container_width=True):
                    conn = sqlite3.connect(DB_FILE)
                    conn.row_factory = sqlite3.Row
                    demo = conn.execute("SELECT id, email, currency FROM users WHERE lower(email) = 'demo@pennywise.app'").fetchone()
                    if not demo:
                        c = conn.cursor()
                        c.execute("INSERT INTO users (email, currency) VALUES ('demo@pennywise.app', 'Rs')")
                        conn.commit()
                        demo_id = str(c.lastrowid)
                    else:
                        demo_id = str(demo["id"])
                    conn.close()
                    st.session_state.user_id = demo_id
                    st.session_state.user_email = "demo@pennywise.app"
                    st.session_state.user_currency = "Rs"
                    seed_sample_data(demo_id, force_reset=True)
                    st.rerun()

        with signup_tab:
            with st.form("signup_form"):
                new_email = st.text_input("Email address", key="su-email")
                new_password = st.text_input("Password", type="password", key="su-pwd", help="At least 8 characters.")
                selected_curr = st.selectbox("Preferred currency", list(CURRENCIES), key="su-curr")
                create_btn = st.form_submit_button("Create account", type="primary", use_container_width=True)
            if create_btn:
                if not new_email.strip():
                    st.error("Please enter a valid email address.")
                else:
                    curr_symbol = CURRENCIES[selected_curr]
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    try:
                        c.execute("INSERT INTO users (email, currency) VALUES (?, ?)", (new_email.strip(), curr_symbol))
                        conn.commit()
                        uid = str(c.lastrowid)
                    except sqlite3.IntegrityError:
                        c.execute("UPDATE users SET currency = ? WHERE lower(email) = lower(?)", (curr_symbol, new_email.strip()))
                        conn.commit()
                        row = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (new_email.strip(),)).fetchone()
                        uid = str(row[0])
                    conn.close()
                    st.session_state.user_id = uid
                    st.session_state.user_email = new_email.strip()
                    st.session_state.user_currency = curr_symbol
                    seed_sample_data(uid, force_reset=True)
                    st.success("Account created successfully!")
                    st.rerun()

def main_app():
    inject_custom_css()
    uid = signed_in_user_id()
    user_email = st.session_state.get("user_email", "User")
    symbol = get_profile_currency(uid, st.session_state.get("user_currency", "Rs"))
    
    # Check if demo has at least 100 transactions, seed if needed
    seed_sample_data(uid, force_reset=False)
    
    # 18 months options for top bar
    today = date.today()
    month_options = [(today.replace(day=1) - pd.DateOffset(months=i)).date() for i in range(18)]
    
    # Header row
    brand_col, month_col, account_col = st.columns([3, 2, 1.8], vertical_alignment="center")
    with brand_col:
        st.markdown('<div class="brand"><span class="brand-mark">eco</span>Pennywise</div>', unsafe_allow_html=True)
    with month_col:
        selected_month_date = st.selectbox(
            "Month",
            month_options,
            format_func=lambda d: d.strftime("%B %Y"),
            label_visibility="collapsed",
            key="top-month-selector",
        )
        selected_month_str = selected_month_date.strftime("%Y-%m")
        month_label = selected_month_date.strftime("%B %Y")
    with account_col:
        av_col, signout_col = st.columns([0.45, 1.55], vertical_alignment="center")
        initial = (user_email[0] if user_email else "U").upper()
        if av_col.button(initial, key="user-square-logo", help="Click to open Settings & account data"):
            st.session_state["main-nav-radio"] = "Settings"
            st.rerun()
        if signout_col.button("Sign out", key="top-signout"):
            st.session_state.clear()
            st.rerun()
            
    st.markdown('<div class="topline"></div>', unsafe_allow_html=True)
    
    # Segmented navigation tabs
    current_tab = st.radio(
        "Navigation",
        ["Overview", "Transactions", "Budgets & goals", "Import & export", "Settings"],
        horizontal=True,
        label_visibility="collapsed",
        key="main-nav-radio",
    )
    
    df = get_transactions_df(uid)
    custom_cats = get_custom_categories(uid)
    all_cats = sorted(set(CATEGORIES + custom_cats))

    # -----------------------------------------------------------------------
    # TAB 1: OVERVIEW
    # -----------------------------------------------------------------------
    if current_tab == "Overview":
        ym = selected_month_str
        m_df = df[df.date.apply(lambda x: x.strftime("%Y-%m") == ym)] if not df.empty else df
        
        inc = m_df.loc[m_df.type == "Income", "amount"].sum() if not m_df.empty else 0.0
        exp = m_df.loc[(m_df.type == "Expense") & (m_df.category != "Savings contribution"), "amount"].sum() if not m_df.empty else 0.0
        net = inc - exp
        
        # Monthly overall budget and calculations
        all_budgets = get_budgets_list(uid, ym)
        overall_budget_row = next((b for b in all_budgets if b["category"] == "All spending"), None)
        total_budget = float(overall_budget_row["amount"]) if overall_budget_row else 0.0
        budget_pct = min(100.0, (exp / total_budget * 100)) if total_budget > 0 else 0.0
        is_overall_overspent = total_budget > 0 and exp > total_budget
        overspent_overall = exp - total_budget if is_overall_overspent else 0.0
        remaining_budget = max(total_budget - exp, 0.0)
        
        # Category budgets check for warnings
        cat_budgets = [b for b in all_budgets if b["category"] != "All spending"]
        exceeded_cat_warnings = []
        for cb in cat_budgets:
            cb_used = m_df.loc[(m_df.type == "Expense") & (m_df.category == cb["category"]) & (m_df.category != "Savings contribution"), "amount"].sum() if not m_df.empty else 0.0
            cb_limit = float(cb["amount"])
            if cb_limit > 0 and cb_used > cb_limit:
                diff = cb_used - cb_limit
                pct = (cb_used / cb_limit) * 100
                exceeded_cat_warnings.append((cb["category"], cb_used, cb_limit, diff, pct))

        # Hero Card
        sub_text = "You're in control. Keep going at your own pace." if net >= 0 else "Spending is above income this month. Review your plan below."
        st.markdown(
            f"""
            <div class="hero">
                <div>
                    <div class="hero-kicker">YOUR MONEY, MADE SIMPLE · {month_label.upper()}</div>
                    <h2>Your spending this month</h2>
                    <div class="hero-value">{money(exp, symbol)}</div>
                    <p>{money(net, symbol)} net savings · {sub_text}</p>
                </div>
                <div class="hero-box">
                    <div style="display:flex;justify-content:space-between;font-size:0.9rem;margin-bottom:8px">
                        <span>Monthly budget used</span>
                        <b style="color:{'#b91c1c' if is_overall_overspent else '#214c3e'}">{(exp / total_budget * 100) if total_budget > 0 else 0:.0f}%</b>
                    </div>
                    <div style="height:10px;background:#c3d8c9;border-radius:10px;margin-bottom:10px;overflow:hidden">
                        <div style="height:10px;width:{min(100.0, (exp / total_budget * 100)) if total_budget > 0 else 0}%;background:{'#dc2626' if is_overall_overspent else '#245f4d'};border-radius:10px"></div>
                    </div>
                    <div style="font-size:0.85rem;color:{'#b91c1c;font-weight:600' if is_overall_overspent else '#507564'}">
                        {f'⚠️ Exceeded by {money(overspent_overall, symbol)}' if is_overall_overspent else f'{money(remaining_budget, symbol)} remaining'}
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        
        # -------------------------------------------------------------------
        # PROMINENT BUDGET WARNING CALLOUTS
        # -------------------------------------------------------------------
        # 1. Overall budget exceeded warning
        if is_overall_overspent:
            st.markdown(
                f"""
                <div class="budget-alert-card">
                    <span style="font-size:1.5rem;line-height:1">🚨</span>
                    <div>
                        <strong style="font-size:1.05rem">Monthly Budget Exceeded:</strong><br/>
                        You have spent <strong>{money(exp, symbol)}</strong>, exceeding your <strong>{money(total_budget, symbol)}</strong> monthly limit by <strong style="color:#b91c1c">+{money(overspent_overall, symbol)}</strong> ({(exp / total_budget * 100):.0f}% of budget used).
                        Please adjust upcoming expenditures or reallocate category limits.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        elif total_budget > 0 and budget_pct >= 80:
            st.markdown(
                f"""
                <div class="budget-warning-card">
                    <span style="font-size:1.5rem;line-height:1">⚠️</span>
                    <div>
                        <strong>Approaching Monthly Budget Limit:</strong><br/>
                        You have used <strong>{budget_pct:.0f}%</strong> of your monthly limit ({money(remaining_budget, symbol)} remaining out of {money(total_budget, symbol)}).
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # 2. Category-specific budget exceeded warnings
        for cat_name, cat_used, cat_lim, cat_diff, cat_pct in exceeded_cat_warnings:
            st.markdown(
                f"""
                <div class="budget-alert-card" style="background:#fff5f5;border-color:#fca5a5">
                    <span style="font-size:1.3rem;line-height:1">⚠️</span>
                    <div>
                        <strong>Category Budget Exceeded for '{html.escape(cat_name)}':</strong><br/>
                        You have spent <strong>{money(cat_used, symbol)}</strong> against your <strong>{money(cat_lim, symbol)}</strong> limit (<strong style="color:#b91c1c">+{money(cat_diff, symbol)} over budget</strong>, {cat_pct:.0f}% used).
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # 3. Deficit warning (expenses > income)
        if exp > inc and exp > 0:
            st.warning(f"Expenses are {money(exp - inc, symbol)} higher than income this month. Review your recent spending before adding more costs.")

        # Month-end forecast
        days_in_month = (date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1) - timedelta(days=1)).day
        forecast = (exp / today.day * days_in_month) if (ym == today.strftime("%Y-%m") and today.day) else exp
        
        # 4 Metric Cards
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Income", money(inc, symbol))
        m2.metric("Expenses", money(exp, symbol))
        m3.metric("Net savings", money(net, symbol))
        m4.metric("Month-end forecast", money(forecast, symbol))
        
        if not m_df.empty:
            cat_exp = m_df[m_df.type == "Expense"].groupby("category", as_index=False).amount.sum().sort_values("amount", ascending=False)
            if not cat_exp.empty:
                top_row = cat_exp.iloc[0]
                pct_share = (top_row.amount / exp * 100) if exp > 0 else 0
                st.caption(f"Highest spending category: **{top_row.category}** · {money(top_row.amount, symbol)} ({pct_share:.0f}% of spending)")
                
        # -------------------------------------------------------------------
        # MULTI-MONTH TREND CHART
        # -------------------------------------------------------------------
        st.markdown("### Monthly cash flow & savings trend")
        chart_col1, chart_col2 = st.columns([3, 1], vertical_alignment="center")
        with chart_col1:
            st.caption("Compare Income, Expenses, and Net Savings across multiple months.")
        with chart_col2:
            time_window = st.selectbox(
                "Time window",
                ["Last 3 Months", "Last 6 Months", "All Recorded Months"],
                index=1,
                label_visibility="collapsed",
                key="trend-time-window",
            )

        if not df.empty:
            df_chart = df[df.category != "Savings contribution"].copy()
            df_chart["YearMonth"] = pd.to_datetime(df_chart.date).dt.strftime("%Y-%m")
            
            # Determine months to include based on user selection
            unique_months = sorted(df_chart["YearMonth"].unique())
            if time_window == "Last 3 Months":
                included_months = unique_months[-3:]
            elif time_window == "Last 6 Months":
                included_months = unique_months[-6:]
            else:
                included_months = unique_months

            df_chart = df_chart[df_chart["YearMonth"].isin(included_months)]
            
            # Aggregate by YearMonth and Type
            agg = df_chart.groupby(["YearMonth", "type"], as_index=False).amount.sum()
            pivot = agg.pivot(index="YearMonth", columns="type", values="amount").fillna(0).reset_index()
            if "Income" not in pivot.columns:
                pivot["Income"] = 0.0
            if "Expense" not in pivot.columns:
                pivot["Expense"] = 0.0
            pivot["Net"] = pivot["Income"] - pivot["Expense"]
            pivot["MonthLabel"] = pd.to_datetime(pivot["YearMonth"] + "-01").dt.strftime("%b %Y")

            # Create rich Plotly figure with Dual traces (Bars + Net Savings Line)
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            
            # Bar 1: Income
            fig.add_trace(
                go.Bar(
                    x=pivot["MonthLabel"],
                    y=pivot["Income"],
                    name="Income",
                    marker_color=ORANGE,
                    hovertemplate="%{x}<br>Income: " + symbol + " %{y:,.0f}<extra></extra>",
                ),
                secondary_y=False,
            )
            # Bar 2: Expenses
            fig.add_trace(
                go.Bar(
                    x=pivot["MonthLabel"],
                    y=pivot["Expense"],
                    name="Expenses",
                    marker_color=GREEN,
                    hovertemplate="%{x}<br>Expenses: " + symbol + " %{y:,.0f}<extra></extra>",
                ),
                secondary_y=False,
            )
            # Line: Net Savings
            fig.add_trace(
                go.Scatter(
                    x=pivot["MonthLabel"],
                    y=pivot["Net"],
                    name="Net Savings",
                    mode="lines+markers",
                    line=dict(color="#1f513f", width=3),
                    marker=dict(size=8, color="#1f513f"),
                    hovertemplate="%{x}<br>Net Savings: " + symbol + " %{y:,.0f}<extra></extra>",
                ),
                secondary_y=True,
            )

            fig.update_layout(
                barmode="group",
                height=320,
                margin=dict(l=10, r=10, t=20, b=10),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                legend=dict(orientation="h", y=1.15, x=0.5, xanchor="center"),
                xaxis=dict(showgrid=False),
                yaxis=dict(showgrid=True, gridcolor="#e8ebe5", title="Income & Expenses"),
                yaxis2=dict(showgrid=False, title="Net Savings", overlaying="y", side="right"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Month-by-month summary table
            with st.expander("View multi-month numerical summary table"):
                summary_table = pivot[["MonthLabel", "Income", "Expense", "Net"]].copy()
                summary_table["Savings Rate"] = (summary_table["Net"] / summary_table["Income"] * 100).fillna(0).apply(lambda v: f"{v:.1f}%")
                summary_table["Income"] = summary_table["Income"].apply(lambda v: money(v, symbol))
                summary_table["Expense"] = summary_table["Expense"].apply(lambda v: money(v, symbol))
                summary_table["Net"] = summary_table["Net"].apply(lambda v: money(v, symbol))
                summary_table.rename(columns={"MonthLabel": "Month", "Expense": "Expenses", "Net": "Net Savings"}, inplace=True)
                st.dataframe(summary_table, use_container_width=True, hide_index=True)

        # Breakdown and Category Budgets
        col_pie, col_budgets = st.columns([1.1, 0.9], gap="medium")
        with col_pie:
            st.markdown("### Where your money goes")
            if not m_df.empty:
                cat_data = m_df[(m_df.type == "Expense") & (m_df.category != "Savings contribution")].groupby("category", as_index=False).amount.sum()
                if not cat_data.empty:
                    donut = px.pie(
                        cat_data,
                        names="category",
                        values="amount",
                        hole=0.68,
                        color_discrete_sequence=[GREEN, ORANGE, "#91ad98", "#dccda5", "#8c87aa", "#5c768d"],
                    )
                    donut.update_layout(height=280, margin=dict(l=5, r=5, t=5, b=5), paper_bgcolor="rgba(0,0,0,0)")
                    st.plotly_chart(donut, use_container_width=True)
                else:
                    st.caption("No expense transactions recorded for this month.")
            else:
                st.caption("Your category breakdown will appear here.")
                
        with col_budgets:
            st.markdown("### Category budget check")
            if cat_budgets:
                for b in cat_budgets:
                    used = m_df.loc[(m_df.type == "Expense") & (m_df.category == b["category"]) & (m_df.category != "Savings contribution"), "amount"].sum() if not m_df.empty else 0.0
                    b_limit = float(b["amount"])
                    is_over = b_limit > 0 and used > b_limit
                    over_amt = used - b_limit if is_over else 0.0

                    # Title row with badge
                    c_head1, c_head2 = st.columns([2.5, 1.5])
                    c_head1.markdown(f"**{b['category']}**")
                    if is_over:
                        c_head2.markdown(f"<span style='color:#dc2626;font-weight:700;font-size:0.8rem'>EXCEEDED (+{money(over_amt, symbol)})</span>", unsafe_allow_html=True)
                    else:
                        c_head2.markdown(f"<span style='color:#507564;font-size:0.8rem'>{money(b_limit - used, symbol)} left</span>", unsafe_allow_html=True)

                    st.write(f"{money(used, symbol)} / {money(b_limit, symbol)}")
                    
                    # Progress bar (red if over)
                    st.progress(min(1.0, used / b_limit) if b_limit > 0 else 0.0)
                    if is_over:
                        st.caption(f"⚠️ Limit breached by {money(over_amt, symbol)}.")
            else:
                st.caption("Set department spending limits in Budgets & goals.")
                
        # Recent Transactions & A Money Moment
        rec_col, moment_col = st.columns([1.3, 0.7], gap="large")
        with rec_col:
            st.markdown(f"### Recent transactions ({len(df)} total recorded)")
            if df.empty:
                st.caption("No saved transactions yet. Add one from Transactions or import a CSV.")
            else:
                for _, r in df.head(8).iterrows():
                    transaction_row_ui(r, symbol, show_actions=False)
                    st.markdown('<div class="txn-row-rule"></div>', unsafe_allow_html=True)
                    
        with moment_col:
            st.markdown("### A money moment")
            if is_overall_overspent:
                advice = f"Your total expenses ({money(exp, symbol)}) exceeded your monthly target by {money(overspent_overall, symbol)}. Take a moment to review where the highest spikes occurred and set realistic caps for next month."
            elif inc and exp > inc:
                advice = f"Expenses are {money(exp - inc, symbol)} above income. Start with the highest expense category and check for recurring costs you can adjust."
            elif exp == 0:
                advice = "No expense transactions this month yet. Add entries as they happen and this check-in can highlight your patterns."
            elif not m_df[(m_df.type == "Expense") & (m_df.category != "Savings contribution")].empty:
                ex_grp = m_df[(m_df.type == "Expense") & (m_df.category != "Savings contribution")].groupby("category").amount.sum().sort_values(ascending=False)
                top_c = ex_grp.index[0]
                top_v = ex_grp.iloc[0]
                sh = (top_v / exp * 100) if exp > 0 else 0
                advice = f"{top_c} is your largest spending category at {money(top_v, symbol)} ({sh:.0f}% of expenses). Scan those transactions for adjustments you want to plan for next month."
            else:
                advice = "You're building a useful record. Check back after a few entries to spot patterns in your spending."
            st.markdown(f'<div class="moment"><strong>Gentle check-in:</strong><br/>{html.escape(advice)}</div>', unsafe_allow_html=True)

    # -----------------------------------------------------------------------
    # TAB 2: TRANSACTIONS
    # -----------------------------------------------------------------------
    elif current_tab == "Transactions":
        st.title("Transactions")
        st.caption(f"{len(df)} total transactions spanning multiple months in your ledger.")
        col_inc, col_exp = st.columns(2, gap="medium")
        
        # Add Income Form
        with col_inc:
            st.subheader("Add income")
            with st.form("add_income_form", clear_on_submit=True):
                a1, a2, a3 = st.columns(3)
                dt_inc = a1.date_input("Date", value=today, key="add-inc-date")
                amt_inc = a2.number_input(f"Amount ({symbol})", min_value=0.01, step=100.0, key="add-inc-amt")
                cat_inc = a3.selectbox("Category", [c for c in all_cats if c != "Savings contribution"], key="add-inc-cat")
                new_cat_inc = st.text_input("Or create a new category (optional)", placeholder="e.g. Dividend, Bonus", key="add-inc-newcat")
                note_inc = st.text_input("Description or notes", placeholder="e.g. Monthly salary", key="add-inc-note")
                sub_inc = st.form_submit_button("Add income", type="primary", use_container_width=True)
            if sub_inc:
                final_cat = new_cat_inc.strip() or cat_inc
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                if new_cat_inc.strip() and final_cat not in all_cats:
                    c.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, final_cat))
                c.execute(
                    "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, dt_inc.isoformat(), amt_inc, "Income", final_cat, note_inc, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                conn.close()
                st.success("Income saved.")
                st.rerun()

        # Add Expense Form with Budget Pre-Check Warning
        with col_exp:
            st.subheader("Add expense")
            with st.form("add_expense_form", clear_on_submit=True):
                b1, b2, b3 = st.columns(3)
                dt_exp = b1.date_input("Date", value=today, key="add-exp-date")
                amt_exp = b2.number_input(f"Amount ({symbol})", min_value=0.01, step=100.0, key="add-exp-amt")
                exp_avail = [c for c in all_cats if c not in ["Salary", "Freelance", "Savings contribution"]]
                cat_exp = b3.selectbox("Category", exp_avail, key="add-exp-cat")
                new_cat_exp = st.text_input("Or create a new category (optional)", placeholder="e.g. Pet care", key="add-exp-newcat")
                note_exp = st.text_input("Description or notes", placeholder="e.g. Weekly groceries", key="add-exp-note")
                sub_exp = st.form_submit_button("Add expense", type="primary", use_container_width=True)
            if sub_exp:
                final_cat = new_cat_exp.strip() or cat_exp
                exp_month = dt_exp.strftime("%Y-%m")
                
                # Check if this expense exceeds budget
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                if new_cat_exp.strip() and final_cat not in all_cats:
                    c.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, final_cat))
                
                # Check category budget
                cat_b = c.execute("SELECT amount FROM budgets WHERE user_id = ? AND month = ? AND category = ?", (uid, exp_month, final_cat)).fetchone()
                cur_cat_spent = c.execute("SELECT sum(amount) FROM transactions WHERE user_id = ? AND strftime('%Y-%m', txn_date) = ? AND category = ? AND kind = 'Expense'", (uid, exp_month, final_cat)).fetchone()[0] or 0.0
                
                c.execute(
                    "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, dt_exp.isoformat(), amt_exp, "Expense", final_cat, note_exp, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
                conn.close()

                if cat_b and (cur_cat_spent + amt_exp) > float(cat_b["amount"]):
                    st.warning(f"⚠️ Budget Alert: This entry brings your '{final_cat}' spending to {money(cur_cat_spent + amt_exp, symbol)}, exceeding your set budget of {money(cat_b['amount'], symbol)}!")
                else:
                    st.success("Expense saved.")
                st.rerun()
                
        # Filter row
        st.markdown("### Ledger entries")
        if df.empty:
            st.info("No recorded transactions yet.")
        else:
            f1, f2, f3, f4 = st.columns([1, 1, 1.2, 1.3])
            start_date = f1.date_input("From", value=today - timedelta(days=90), key="filter-from")
            end_date = f2.date_input("To", value=today, key="filter-to")
            selected_filter_cat = f3.selectbox("Category", ["All categories"] + all_cats, key="filter-cat")
            search_query = f4.text_input("Search description", placeholder="Filter by notes...", key="filter-search")
            
            filtered = df[(df.date >= start_date) & (df.date <= end_date)]
            if selected_filter_cat != "All categories":
                filtered = filtered[filtered.category == selected_filter_cat]
            if search_query.strip():
                filtered = filtered[filtered.description.astype(str).str.contains(search_query.strip(), case=False, na=False)]
                
            st.caption(f"{len(filtered)} recorded entr{'y' if len(filtered) == 1 else 'ies'} found in selected period.")
            
            # Edit Transaction Form
            edit_id = st.session_state.get("edit_transaction_id")
            if edit_id:
                row_edit = df[df.id == edit_id]
                if not row_edit.empty:
                    r_curr = row_edit.iloc[0]
                    st.markdown("#### Edit transaction")
                    with st.form("edit_form"):
                        e1, e2, e3 = st.columns(3)
                        new_dt = e1.date_input("Date", value=r_curr.date)
                        new_amt = e2.number_input(f"Amount ({symbol})", min_value=0.01, value=float(r_curr.amount), step=100.0)
                        new_kind = e3.selectbox("Type", ["Expense", "Income"], index=0 if r_curr.type == "Expense" else 1)
                        cat_idx = all_cats.index(r_curr.category) if r_curr.category in all_cats else 0
                        new_cat = st.selectbox("Category", all_cats, index=cat_idx)
                        new_note = st.text_input("Description", value=r_curr.description or "")
                        btn_save = st.form_submit_button("Save changes", type="primary")
                    if btn_save:
                        conn = sqlite3.connect(DB_FILE)
                        if pd.notna(r_curr.get("goal_id")) and r_curr.category == "Savings contribution":
                            gid = int(r_curr.goal_id)
                            conn.row_factory = sqlite3.Row
                            g = conn.execute("SELECT saved FROM goals WHERE id = ? AND user_id = ?", (gid, uid)).fetchone()
                            if g:
                                delta = (new_amt - r_curr.amount) if new_cat == "Savings contribution" else -r_curr.amount
                                conn.execute("UPDATE goals SET saved = ? WHERE id = ? AND user_id = ?", (max(0.0, float(g["saved"]) + delta), gid, uid))
                        conn.execute(
                            "UPDATE transactions SET txn_date = ?, amount = ?, kind = ?, category = ?, note = ? WHERE id = ? AND user_id = ?",
                            (new_dt.isoformat(), new_amt, new_kind, new_cat, new_note, edit_id, uid),
                        )
                        conn.commit()
                        conn.close()
                        st.session_state.edit_transaction_id = None
                        st.toast("Changes saved.")
                        st.rerun()
                    if st.button("Cancel editing"):
                        st.session_state.edit_transaction_id = None
                        st.rerun()
                        
            # Transaction list
            for _, r in filtered.iterrows():
                transaction_row_ui(r, symbol, show_actions=True)
                st.markdown('<div class="txn-row-rule"></div>', unsafe_allow_html=True)

    # -----------------------------------------------------------------------
    # TAB 3: BUDGETS & GOALS
    # -----------------------------------------------------------------------
    elif current_tab == "Budgets & goals":
        st.title("Budgets & savings goals")
        col_b, col_g = st.columns(2, gap="large")
        
        with col_b:
            st.subheader("Monthly budgets")
            b_month = st.date_input("Budget month", value=selected_month_date, key="b-month-input")
            b_ym = b_month.strftime("%Y-%m")
            
            with st.form("budget_create_form"):
                b_cat = st.selectbox("Budget for", ["All spending"] + [c for c in all_cats if c != "Savings contribution"])
                b_lim = st.number_input(f"Monthly limit ({symbol})", min_value=0.0, step=1000.0)
                save_b = st.form_submit_button("Save budget", type="primary")
            if save_b:
                conn = sqlite3.connect(DB_FILE)
                conn.execute(
                    "INSERT OR REPLACE INTO budgets (user_id, month, category, amount) VALUES (?, ?, ?, ?)",
                    (uid, b_ym, b_cat, b_lim),
                )
                conn.commit()
                conn.close()
                st.success("Budget saved.")
                st.rerun()
                
            st.markdown("#### Configured budgets")
            budgets_all = get_budgets_list(uid)
            if not budgets_all:
                st.caption("No budgets recorded yet.")
            else:
                for b in budgets_all:
                    c1, c2 = st.columns([3, 1])
                    c1.write(f"**{b['month']}** · {b['category']} · {money(b['amount'], symbol)}")
                    if c2.button("Delete", key=f"del-bdg-{b['id']}"):
                        conn = sqlite3.connect(DB_FILE)
                        conn.execute("DELETE FROM budgets WHERE id = ? AND user_id = ?", (b["id"], uid))
                        conn.commit()
                        conn.close()
                        st.rerun()

        with col_g:
            st.subheader("Savings goals")
            with st.form("goal_create_form"):
                g_name = st.text_input("Goal name", placeholder="e.g. Travel, Emergency reserve")
                g_target = st.number_input(f"Target amount ({symbol})", min_value=1.0, step=1000.0)
                g_initial = st.number_input(f"Already saved ({symbol})", min_value=0.0, step=500.0)
                g_due = st.date_input("Target date", value=today + timedelta(days=90))
                save_g = st.form_submit_button("Create savings goal", type="primary")
            if save_g and g_name.strip():
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute(
                    "INSERT INTO goals (user_id, name, target, saved, due_date) VALUES (?, ?, ?, ?, ?)",
                    (uid, g_name.strip(), g_target, g_initial, g_due.isoformat()),
                )
                new_gid = c.lastrowid
                if g_initial > 0:
                    c.execute(
                        "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at, goal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (uid, today.isoformat(), g_initial, "Expense", "Savings contribution", f"Initial contribution · {g_name.strip()}", datetime.now(timezone.utc).isoformat(), new_gid),
                    )
                conn.commit()
                conn.close()
                st.success("Savings goal created.")
                st.rerun()
                
            st.markdown("#### Active goals")
            goals_all = get_goals_list(uid)
            if not goals_all:
                st.caption("No savings goals yet.")
            else:
                for g in goals_all:
                    st.markdown(f"**{g['name']}** · {money(g['saved'], symbol)} of {money(g['target'], symbol)}")
                    st.progress(min(1.0, float(g["saved"]) / float(g["target"])) if float(g["target"]) > 0 else 0.0)
                    with st.form(f"contrib-{g['id']}"):
                        ca1, ca2, ca3 = st.columns([1.5, 1.5, 1])
                        add_amt = ca1.number_input(f"Add ({symbol})", min_value=0.01, step=100.0, key=f"ca-amt-{g['id']}")
                        add_dt = ca2.date_input("Date", value=today, key=f"ca-dt-{g['id']}")
                        add_ok = ca3.form_submit_button("Contribute")
                    if add_ok and add_amt > 0:
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        new_total = float(g["saved"]) + add_amt
                        c.execute("UPDATE goals SET saved = ? WHERE id = ? AND user_id = ?", (new_total, g["id"], uid))
                        c.execute(
                            "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at, goal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (uid, add_dt.isoformat(), add_amt, "Expense", "Savings contribution", f"Contribution · {g['name']}", datetime.now(timezone.utc).isoformat(), g["id"]),
                        )
                        conn.commit()
                        conn.close()
                        st.success("Contribution added!")
                        st.rerun()
                    if st.button("Delete goal", key=f"del-goal-{g['id']}"):
                        conn = sqlite3.connect(DB_FILE)
                        conn.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (g["id"], uid))
                        conn.commit()
                        conn.close()
                        st.rerun()

    # -----------------------------------------------------------------------
    # TAB 4: IMPORT & EXPORT
    # -----------------------------------------------------------------------
    elif current_tab == "Import & export":
        st.title("Import & export")
        st.subheader("Import bank transactions")
        uploaded_csv = st.file_uploader("Upload bank statement CSV", type=["csv"])
        if uploaded_csv:
            try:
                raw_df = pd.read_csv(uploaded_csv)
                raw_df.columns = [str(col).strip().lower().replace(" ", "_") for col in raw_df.columns]
                
                date_col = next((c for c in ["date", "transaction_date", "txn_date", "posted_date"] if c in raw_df), None)
                amount_col = next((c for c in ["amount", "value"] if c in raw_df), None)
                desc_col = next((c for c in ["description", "note", "notes", "details", "merchant"] if c in raw_df), None)
                type_col = next((c for c in ["type", "transaction_type", "kind"] if c in raw_df), None)
                cat_col = "category" if "category" in raw_df else None
                debit_col = next((c for c in ["debit", "withdrawal", "money_out"] if c in raw_df), None)
                credit_col = next((c for c in ["credit", "deposit", "money_in"] if c in raw_df), None)
                
                valid_entries = []
                if date_col and (amount_col or debit_col or credit_col):
                    for _, row in raw_df.iterrows():
                        try:
                            d_val = pd.to_datetime(row[date_col]).date()
                            if amount_col:
                                clean_val = float(str(row[amount_col]).replace(",", "").replace(symbol, "").replace("$", "").strip())
                                kind_val = str(row[type_col]).title() if (type_col and pd.notna(row[type_col])) else ("Expense" if clean_val < 0 else "Income")
                                val = abs(clean_val)
                            else:
                                deb = float(row[debit_col]) if (debit_col and pd.notna(row[debit_col])) else 0.0
                                cred = float(row[credit_col]) if (credit_col and pd.notna(row[credit_col])) else 0.0
                                kind_val = "Expense" if deb > 0 else "Income"
                                val = deb if deb > 0 else cred
                            if val > 0:
                                cat_val = str(row[cat_col]) if (cat_col and pd.notna(row[cat_col])) else "Other"
                                note_val = str(row[desc_col]) if (desc_col and pd.notna(row[desc_col])) else "Imported from CSV"
                                valid_entries.append((d_val.isoformat(), val, kind_val if kind_val in ["Income", "Expense"] else "Expense", cat_val, note_val))
                        except Exception:
                            pass
                    st.write(f"Preview: **{len(valid_entries)}** valid rows detected out of {len(raw_df)} total rows.")
                    if valid_entries and st.button("Confirm and import transactions", type="primary"):
                        conn = sqlite3.connect(DB_FILE)
                        now_str = datetime.now(timezone.utc).isoformat()
                        for v_date, v_amt, v_kind, v_cat, v_note in valid_entries:
                            conn.execute(
                                "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (uid, v_date, v_amt, v_kind, v_cat, v_note, now_str),
                            )
                        conn.commit()
                        conn.close()
                        st.success(f"Successfully imported {len(valid_entries)} transactions!")
                        st.rerun()
                else:
                    st.error("The CSV must include a date column and an amount, debit, or credit column.")
            except Exception as e:
                st.error(f"Could not parse CSV: {e}")
                
        st.markdown("---")
        st.subheader("Export")
        if df.empty:
            st.info("Add transactions to enable data exports.")
        else:
            e_col1, e_col2 = st.columns(2)
            with e_col1:
                st.markdown("#### Transaction ledger export")
                exp_range = st.date_input("Date range", value=(today.replace(day=1), today), key="exp-range-inp")
                if isinstance(exp_range, (list, tuple)) and len(exp_range) == 2:
                    st_date, end_dt = exp_range
                    exp_df = df[(df.date >= st_date) & (df.date <= end_dt)].copy()
                    st.download_button(
                        "Export transactions as CSV",
                        exp_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="pennywise-transactions.csv",
                        mime="text/csv",
                    )
            with e_col2:
                st.markdown(f"#### Monthly summary ({month_label})")
                ym_cur = selected_month_str
                cur_m_df = df[df.date.apply(lambda x: x.strftime("%Y-%m") == ym_cur)]
                inc_val = cur_m_df.loc[cur_m_df.type == "Income", "amount"].sum() if not cur_m_df.empty else 0.0
                exp_val = cur_m_df.loc[cur_m_df.type == "Expense", "amount"].sum() if not cur_m_df.empty else 0.0
                summary_csv = f"Month,{ym_cur}\nIncome,{inc_val}\nExpenses,{exp_val}\nNet savings,{inc_val - exp_val}\n"
                st.download_button(
                    "Export monthly summary CSV",
                    summary_csv.encode("utf-8-sig"),
                    file_name=f"pennywise-summary-{ym_cur}.csv",
                    mime="text/csv",
                )
                
                try:
                    from reportlab.lib import colors
                    from reportlab.lib.pagesizes import letter
                    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
                    from reportlab.lib.styles import getSampleStyleSheet
                    
                    pdf_buf = io.BytesIO()
                    doc = SimpleDocTemplate(pdf_buf, pagesize=letter)
                    styles = getSampleStyleSheet()
                    story = [
                        Paragraph(f"Pennywise Monthly Summary · {month_label}", styles["Title"]),
                        Spacer(1, 14),
                        Paragraph(f"Income: {money(inc_val, symbol)} | Spending: {money(exp_val, symbol)} | Net savings: {money(inc_val - exp_val, symbol)}", styles["BodyText"]),
                        Spacer(1, 16),
                    ]
                    table_rows = [["Date", "Type", "Category", "Description", "Amount"]]
                    for _, r in cur_m_df.iterrows():
                        table_rows.append([str(r.date), str(r.type), str(r.category), str(r.description or "")[:35], f"{symbol} {r.amount:,.2f}"])
                    t = Table(table_rows, repeatRows=1, colWidths=[70, 60, 95, 200, 80])
                    t.setStyle(
                        TableStyle([
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(GREEN)),
                            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dce5dd")),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f7f3")]),
                            ("FONTSIZE", (0, 0), (-1, -1), 8),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ])
                    )
                    story.append(t)
                    doc.build(story)
                    st.download_button(
                        "Download monthly summary as PDF",
                        pdf_buf.getvalue(),
                        file_name=f"pennywise-summary-{ym_cur}.pdf",
                        mime="application/pdf",
                    )
                except Exception as ex:
                    st.caption(f"PDF export note: {ex}")

    # -----------------------------------------------------------------------
    # TAB 5: SETTINGS & ACCOUNT MANAGEMENT (INCLUDING DELETE ACCOUNT)
    # -----------------------------------------------------------------------
    elif current_tab == "Settings":
        st.title("Settings & account data")
        
        # Display Currency
        st.markdown("### Display currency")
        cur_label = next((k for k, v in CURRENCIES.items() if v == symbol), "PKR · Rs")
        new_curr_select = st.selectbox("Preferred display currency", list(CURRENCIES), index=list(CURRENCIES).index(cur_label))
        if CURRENCIES[new_curr_select] != symbol:
            update_profile_currency(uid, CURRENCIES[new_curr_select])
            st.session_state.user_currency = CURRENCIES[new_curr_select]
            st.success("Currency updated!")
            st.rerun()
            
        st.markdown("---")
        # Custom categories
        st.markdown("### Custom categories")
        with st.form("add_cat_form"):
            c_name = st.text_input("New category name")
            c_add = st.form_submit_button("Add category")
        if c_add and c_name.strip():
            if c_name.strip() in all_cats:
                st.error("That category already exists.")
            else:
                conn = sqlite3.connect(DB_FILE)
                conn.execute("INSERT INTO categories (user_id, name) VALUES (?, ?)", (uid, c_name.strip()))
                conn.commit()
                conn.close()
                st.success("Category added.")
                st.rerun()
                
        if custom_cats:
            st.markdown("#### Rename custom category")
            with st.form("rename_cat_form"):
                old_c = st.selectbox("Select category to rename", custom_cats)
                new_c = st.text_input("New name", value=old_c)
                ren_btn = st.form_submit_button("Save category name")
            if ren_btn:
                target_new = new_c.strip()
                if not target_new:
                    st.error("Category name cannot be empty.")
                elif target_new != old_c and target_new in all_cats:
                    st.error("A category with that name already exists.")
                elif target_new != old_c:
                    conn = sqlite3.connect(DB_FILE)
                    conn.execute("UPDATE transactions SET category = ? WHERE user_id = ? AND category = ?", (target_new, uid, old_c))
                    conn.execute("UPDATE budgets SET category = ? WHERE user_id = ? AND category = ?", (target_new, uid, old_c))
                    conn.execute("UPDATE categories SET name = ? WHERE user_id = ? AND name = ?", (target_new, uid, old_c))
                    conn.commit()
                    conn.close()
                    st.success(f"Renamed '{old_c}' to '{target_new}'.")
                    st.rerun()

        st.markdown("---")
        # Backup and restore
        st.markdown("### Backup and restore")
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        tx_rows = [dict(r) for r in conn.execute("SELECT * FROM transactions WHERE user_id = ?", (uid,)).fetchall()]
        bdg_rows = [dict(r) for r in conn.execute("SELECT * FROM budgets WHERE user_id = ?", (uid,)).fetchall()]
        goal_rows = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE user_id = ?", (uid,)).fetchall()]
        cat_rows = [dict(r) for r in conn.execute("SELECT * FROM categories WHERE user_id = ?", (uid,)).fetchall()]
        conn.close()
        
        backup_payload = {
            "users": [{"email": user_email, "currency": symbol}],
            "transactions": tx_rows,
            "budgets": bdg_rows,
            "goals": goal_rows,
            "categories": cat_rows,
        }
        st.download_button(
            "Download account backup (JSON)",
            json.dumps(backup_payload, indent=2).encode(),
            file_name="pennywise-backup.json",
            mime="application/json",
            type="primary",
        )
        
        uploaded_backup = st.file_uploader("Restore backup (JSON)", type=["json"], key="backup-restore-file")
        if uploaded_backup and st.button("Restore backup now"):
            try:
                data = json.loads(uploaded_backup.getvalue())
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("DELETE FROM transactions WHERE user_id = ?", (uid,))
                c.execute("DELETE FROM budgets WHERE user_id = ?", (uid,))
                c.execute("DELETE FROM goals WHERE user_id = ?", (uid,))
                c.execute("DELETE FROM categories WHERE user_id = ?", (uid,))
                
                goal_map = {}
                for g in data.get("goals", []):
                    c.execute("INSERT INTO goals (user_id, name, target, saved, due_date) VALUES (?, ?, ?, ?, ?)", (uid, g["name"], g["target"], g.get("saved", 0), g.get("due_date", "")))
                    if "id" in g:
                        goal_map[g["id"]] = c.lastrowid
                for t in data.get("transactions", []):
                    mapped_gid = goal_map.get(t.get("goal_id")) if t.get("goal_id") else None
                    c.execute(
                        "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at, goal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (uid, t["txn_date"], t["amount"], t["kind"], t["category"], t.get("note", ""), t.get("created_at", datetime.now(timezone.utc).isoformat()), mapped_gid),
                    )
                for b in data.get("budgets", []):
                    c.execute("INSERT OR REPLACE INTO budgets (user_id, month, category, amount) VALUES (?, ?, ?, ?)", (uid, b["month"], b.get("category", "All spending"), b["amount"]))
                for cat in data.get("categories", []):
                    c.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, cat["name"]))
                conn.commit()
                conn.close()
                st.success("Backup restored successfully!")
                st.rerun()
            except Exception as ex:
                st.error(f"Failed to restore backup: {ex}")

        # -------------------------------------------------------------------
        # DANGER ZONE: DELETE ACCOUNT
        # -------------------------------------------------------------------
        st.markdown("---")
        st.markdown(
            """
            <div class="danger-card">
                <h3 style="color:#b91c1c;margin-top:0">Danger Zone: Delete Account</h3>
                <p style="color:#7f1d1d;font-size:0.92rem;margin-bottom:12px">
                    Permanently delete your Pennywise account. This will completely remove:
                </p>
                <ul style="color:#7f1d1d;font-size:0.88rem;margin-bottom:16px;line-height:1.6">
                    <li>All income and expense transaction records</li>
                    <li>All monthly budgets and category spending limits</li>
                    <li>All savings goals and linked milestone contributions</li>
                    <li>All custom categories and profile settings</li>
                </ul>
                <p style="color:#991b1b;font-weight:600;font-size:0.9rem">
                    ⚠️ This action is permanent and cannot be undone.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        
        with st.expander("I want to delete my account"):
            st.warning("Are you sure you want to delete your account? All your financial history will be permanently erased.")
            confirm_check = st.checkbox("Yes, I understand that this action is irreversible and all my records will be wiped.", key="confirm-delete-check")
            
            if confirm_check:
                if st.button("Delete my account permanently", type="primary", key="btn-delete-account"):
                    success = delete_user_account(uid, user_email)
                    if success:
                        st.session_state.clear()
                        st.success("Your account and all records have been deleted.")
                        st.rerun()

def main():
    if "user_id" not in st.session_state:
        auth_view()
    else:
        main_app()

if __name__ == "__main__":
    main()
