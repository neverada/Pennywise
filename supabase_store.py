"""Supabase Auth user-scoped storage for Pennywise (no SQLite fallback).

The SQL in ``supabase_schema.sql`` enforces ownership with row-level security.
Use an anon/publishable key and a *per Streamlit session* authenticated client,
never a service-role key or a process-wide cached client.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any


class RemoteStorageError(RuntimeError):
    """A remote operation failed; no local write/fallback should be attempted."""


PAGE_SIZE = 500
MAX_BACKUP_ROWS = 20_000
MAX_IMPORT_ROWS = 2_000
MAX_BACKUP_BYTES = 20 * 1024 * 1024
CURRENCIES = {"Rs", "$", "€", "£", "₹", "د.إ"}
BACKUP_TABLES = ("transactions", "budgets", "goals", "categories")


def _amount(value: Any, label: str, *, positive: bool) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Backup contains an invalid {label}") from exc
    if not math.isfinite(amount) or amount < 0 or (positive and amount == 0):
        raise ValueError(f"Backup contains an invalid {label}")
    return amount


def _text(value: Any, label: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"Backup contains an invalid {label}")
    return value


def validate_backup(data: Any) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Check and strip all untrusted IDs/extra fields before an atomic restore.

    Old SQLite JSON backups and new Supabase JSON backups have the same four
    data arrays. Source emails/user IDs/passwords are never used for ownership.
    A missing array is an error, NOT an instruction to erase that table.
    """
    if not isinstance(data, dict):
        raise ValueError("Backup must be a JSON object")
    for field in BACKUP_TABLES:
        rows = data.get(field)
        if not isinstance(rows, list) or len(rows) > MAX_BACKUP_ROWS:
            raise ValueError(f"Backup {field} must be a list of at most {MAX_BACKUP_ROWS} entries")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Backup {field} must contain objects")

    currency = None
    if "users" in data:
        users = data["users"]
        if not isinstance(users, list) or len(users) > 1 or any(not isinstance(u, dict) for u in users):
            raise ValueError("Backup must contain at most one source user")
        if users and "currency" in users[0]:
            currency = users[0]["currency"]
            if currency not in CURRENCIES:
                raise ValueError("Backup contains an unsupported currency")

    payload: dict[str, list[dict[str, Any]]] = {table: [] for table in BACKUP_TABLES}
    goal_ids: set[str] = set()
    for row in data["goals"]:
        old_id = row.get("id")
        if old_id is not None:
            try:
                old_id = str(int(old_id))
                if int(old_id) <= 0 or old_id in goal_ids:
                    raise ValueError
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Backup contains a duplicate or invalid goal ID") from exc
            goal_ids.add(old_id)
        due_date = row.get("due_date") or ""
        if not isinstance(due_date, str):
            raise ValueError("Backup contains an invalid goal date")
        if due_date:
            try:
                date.fromisoformat(due_date)
            except ValueError as exc:
                raise ValueError("Backup contains an invalid goal date") from exc
        payload["goals"].append({
            "id": old_id, "name": _text(row.get("name"), "goal name"),
            "target": _amount(row.get("target"), "goal target", positive=True),
            "saved": _amount(row.get("saved", 0), "goal balance", positive=False),
            "due_date": due_date,
        })

    for row in data["transactions"]:
        try:
            txn_date = date.fromisoformat(str(row["txn_date"])).isoformat()
        except (KeyError, ValueError) as exc:
            raise ValueError("Backup contains an invalid transaction date") from exc
        kind = row.get("kind")
        if kind not in ("Income", "Expense"):
            raise ValueError("Backup contains an invalid transaction type")
        goal_id = row.get("goal_id")
        if goal_id is not None:
            try:
                goal_id = str(int(goal_id))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Backup contains an invalid goal reference") from exc
            if goal_id not in goal_ids:
                raise ValueError("Backup refers to a missing savings goal")
        created_at = row.get("created_at") or None
        if created_at is not None:
            if not isinstance(created_at, str):
                raise ValueError("Backup contains an invalid creation time")
            try:
                datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Backup contains an invalid creation time") from exc
        note = row.get("note") or ""
        if not isinstance(note, str) or len(note) > 10_000:
            raise ValueError("Backup contains an invalid transaction note")
        payload["transactions"].append({
            "txn_date": txn_date,
            "amount": _amount(row.get("amount"), "transaction amount", positive=True),
            "kind": kind, "category": _text(row.get("category"), "transaction category"),
            "note": note, "goal_id": goal_id, "created_at": created_at,
        })

    budget_keys: set[tuple[str, str]] = set()
    for row in data["budgets"]:
        month = row.get("month")
        category = _text(row.get("category", "All spending"), "budget category")
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("Backup contains an invalid budget month")
        if (month, category) in budget_keys:
            raise ValueError("Backup contains duplicate budgets")
        budget_keys.add((month, category))
        payload["budgets"].append({
            "month": month, "category": category,
            "amount": _amount(row.get("amount"), "budget amount", positive=False),
        })

    names: set[str] = set()
    for row in data["categories"]:
        name = _text(row.get("name"), "category name")
        if name in names:
            raise ValueError("Backup contains duplicate categories")
        names.add(name)
        payload["categories"].append({"name": name})
    return payload, currency


class SupabaseStore:
    def __init__(self, client: Any, user_id: str):
        if not client or not user_id:
            raise ValueError("An authenticated Supabase client and user ID are required")
        try:
            session = client.auth.get_session()
        except Exception as exc:
            raise RemoteStorageError("Could not verify the Supabase session. Sign in again.") from exc
        if not session or not session.user or str(session.user.id) != str(user_id):
            raise RemoteStorageError("Supabase session does not match this account. Sign in again.")
        self.client = client
        self.user_id = str(user_id)

    def _execute(self, query: Any, action: str) -> list[dict[str, Any]]:
        try:
            return query.execute().data or []
        except Exception as exc:
            raise RemoteStorageError(
                f"Could not {action} in Supabase. Check your connection and apply supabase_schema.sql."
            ) from exc

    def _rpc(self, name: str, params: dict[str, Any], action: str) -> Any:
        try:
            return self.client.rpc(name, params).execute().data
        except Exception as exc:
            raise RemoteStorageError(
                f"Could not {action} in Supabase. Check your connection and apply supabase_schema.sql."
            ) from exc

    def _all(self, table: str, columns: str, month: str | None = None) -> list[dict[str, Any]]:
        """Page through PostgREST's 1,000-row default limit, with stable ordering."""
        rows: list[dict[str, Any]] = []
        for offset in range(0, MAX_BACKUP_ROWS + PAGE_SIZE, PAGE_SIZE):
            try:
                query = self.client.table(table).select(columns).eq("user_id", self.user_id)
                if month:
                    query = query.eq("month", month)
                query = query.order("id").range(offset, offset + PAGE_SIZE - 1)
            except Exception as exc:
                raise RemoteStorageError(f"Could not load {table} from Supabase") from exc
            batch = self._execute(query, f"load {table}")
            rows.extend(batch)
            if len(rows) > MAX_BACKUP_ROWS:
                raise RemoteStorageError(f"Too many {table} rows to load safely. Export in smaller batches.")
            if len(batch) < PAGE_SIZE:
                return rows
        raise RemoteStorageError(f"Too many {table} rows to load safely. Export in smaller batches.")

    def currency(self, default: str = "Rs") -> str:
        rows = self._execute(
            self.client.table("profiles").select("currency").eq("id", self.user_id),
            "load your profile",
        )
        if rows:
            return rows[0]["currency"]
        self.set_currency(default)
        return default

    def set_currency(self, currency: str) -> None:
        if currency not in CURRENCIES:
            raise ValueError("Unsupported currency")
        self._execute(
            self.client.table("profiles").upsert(
                {"id": self.user_id, "currency": currency}, on_conflict="id"
            ), "save your currency",
        )

    def transactions(self) -> list[dict[str, Any]]:
        return self._all("transactions", "id,txn_date,amount,kind,category,note,goal_id")

    def budgets(self, month: str | None = None) -> list[dict[str, Any]]:
        return self._all("budgets", "id,month,category,amount", month)

    def goals(self) -> list[dict[str, Any]]:
        return self._all("goals", "id,name,target,saved,due_date")

    def categories(self) -> list[str]:
        return sorted(row["name"] for row in self._all("categories", "id,name"))

    def add_category(self, name: str) -> None:
        if not name.strip():
            raise ValueError("Category name cannot be empty")
        self._execute(
            self.client.table("categories").upsert(
                {"user_id": self.user_id, "name": name}, on_conflict="user_id,name"
            ), "save category",
        )

    def rename_category(self, old_name: str, new_name: str) -> None:
        self._rpc(
            "rename_user_category", {"p_old_name": old_name, "p_new_name": new_name},
            "rename category",
        )

    def add_transaction(
        self, txn_date: date | str, amount: float, kind: str, category: str,
        note: str = "",
    ) -> None:
        if not math.isfinite(float(amount)) or amount <= 0 or kind not in ("Income", "Expense"):
            raise ValueError("A transaction must have a positive amount and a valid type")
        self._execute(
            self.client.table("transactions").insert({
                "user_id": self.user_id,
                "txn_date": txn_date.isoformat() if isinstance(txn_date, date) else txn_date,
                "amount": float(amount), "kind": kind, "category": category, "note": note,
            }), "save transaction",
        )

    def edit_transaction(
        self, txn_id: int, txn_date: date, amount: float, kind: str, category: str, note: str,
    ) -> None:
        if not math.isfinite(float(amount)) or amount <= 0 or kind not in ("Income", "Expense"):
            raise ValueError("A transaction must have a positive amount and a valid type")
        self._rpc("update_user_transaction", {
            "p_transaction_id": int(txn_id), "p_txn_date": txn_date.isoformat(),
            "p_amount": float(amount), "p_kind": kind, "p_category": category, "p_note": note,
        }, "edit transaction")

    def delete_transaction(self, txn_id: int) -> None:
        self._rpc("delete_user_transaction", {"p_transaction_id": int(txn_id)}, "delete transaction")

    def save_budget(self, month: str, category: str, amount: float) -> None:
        if not math.isfinite(float(amount)) or amount < 0:
            raise ValueError("Budget must be nonnegative")
        self._execute(
            self.client.table("budgets").upsert({
                "user_id": self.user_id, "month": month, "category": category, "amount": float(amount),
            }, on_conflict="user_id,month,category"), "save budget",
        )

    def delete_budget(self, budget_id: int) -> None:
        self._execute(
            self.client.table("budgets").delete().eq("id", int(budget_id)).eq("user_id", self.user_id),
            "delete budget",
        )

    def create_goal(
        self, name: str, target: float, saved: float, due_date: date, today: date,
    ) -> None:
        if not math.isfinite(float(target)) or target <= 0 or not math.isfinite(float(saved)) or saved < 0:
            raise ValueError("Goal amounts must be valid and nonnegative")
        # The RPC keeps the initial contribution and goal balance in one DB transaction.
        self._rpc("create_user_goal", {
            "p_name": name, "p_target": float(target), "p_saved": float(saved),
            "p_due_date": due_date.isoformat(), "p_today": today.isoformat(),
        }, "create goal")

    def contribute_goal(self, goal_id: int, amount: float, txn_date: date) -> None:
        if not math.isfinite(float(amount)) or amount <= 0:
            raise ValueError("Contribution amount must be positive")
        self._rpc("add_goal_contribution", {
            "p_goal_id": int(goal_id), "p_amount": float(amount), "p_txn_date": txn_date.isoformat(),
        }, "add goal contribution")

    def delete_goal(self, goal_id: int) -> None:
        self._execute(
            self.client.table("goals").delete().eq("id", int(goal_id)).eq("user_id", self.user_id),
            "delete goal",
        )

    def import_transactions(self, entries: list[tuple[str, float, str, str, str]]) -> None:
        if not 0 < len(entries) <= MAX_IMPORT_ROWS:
            raise ValueError(f"Import must contain 1–{MAX_IMPORT_ROWS} rows")
        # One RPC = one transaction. An invalid row cannot leave a partial import.
        rows = [
            {"txn_date": d, "amount": float(amt), "kind": kind, "category": cat, "note": note}
            for d, amt, kind, cat, note in entries
        ]
        self._rpc("import_user_transactions", {"p_rows": rows}, "import transactions")

    def backup(self, email: str, currency: str) -> dict[str, Any]:
        return {
            "users": [{"email": email, "currency": currency}],
            "transactions": self._all("transactions", "*"),
            "budgets": self._all("budgets", "*"),
            "goals": self._all("goals", "*"),
            "categories": self._all("categories", "*"),
        }

    def restore_backup(self, data: dict[str, Any]) -> str | None:
        """Atomically replace this authenticated account's rows; never trust source IDs."""
        payload, currency = validate_backup(data)
        self._rpc("restore_account_backup", {
            "p_transactions": payload["transactions"], "p_budgets": payload["budgets"],
            "p_goals": payload["goals"], "p_categories": payload["categories"],
            "p_currency": currency,
        }, "restore account backup")
        return currency

    def delete_account(self) -> None:
        # SECURITY DEFINER function deletes auth.users (with cascading account rows).
        # Never claim success if the function isn't installed or doesn't execute.
        self._rpc("delete_my_account", {}, "delete account")
