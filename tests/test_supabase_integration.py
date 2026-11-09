"""Offline app/SDK-contract tests. These DO NOT prove a hosted Supabase deployment works."""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from supabase_store import RemoteStorageError, SupabaseStore, validate_backup


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class MemoryService:
    """Tiny fake PostgREST/Auth service shared by independent browser sessions."""

    def __init__(self):
        self.users = {}
        self.rows = {table: [] for table in ("profiles", "transactions", "budgets", "goals", "categories")}
        self.next_id = {table: 1 for table in self.rows}
        self.calls = []
        self.fail_reads = False
        self.fail_writes = False
        self.confirmation_required = False

    def create_client(self, url, key):
        assert url == "https://example.supabase.co"
        assert key == "test-anon-key"
        client = MemoryClient(self)
        self.calls.append(("new-client", id(client)))
        return client


class MemoryAuth:
    def __init__(self, client):
        self.client = client
        self.session = None

    @property
    def user(self):
        return self.session.user if self.session else None

    def sign_up(self, credentials):
        email = credentials["email"].lower()
        if email in self.client.service.users:
            raise ValueError("Account already exists")
        user = SimpleNamespace(
            id=str(uuid.uuid5(uuid.NAMESPACE_DNS, email)), email=email,
            user_metadata=credentials.get("options", {}).get("data", {}),
        )
        self.client.service.users[email] = (credentials["password"], user)
        self.session = None if self.client.service.confirmation_required else SimpleNamespace(user=user)
        return SimpleNamespace(user=user, session=self.session)

    def sign_in_with_password(self, credentials):
        pair = self.client.service.users.get(credentials["email"].lower())
        if not pair or pair[0] != credentials["password"]:
            raise ValueError("Invalid login")
        self.session = SimpleNamespace(user=pair[1])
        return SimpleNamespace(user=pair[1], session=self.session)

    def get_session(self):
        return self.session

    def sign_out(self):
        self.session = None


class MemoryClient:
    def __init__(self, service):
        self.service = service
        self.auth = MemoryAuth(self)

    def table(self, name):
        return MemoryQuery(self, name)

    def rpc(self, name, params):
        return MemoryRpc(self, name, params)


class MemoryQuery:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.op = "select"
        self.payload = None
        self.filters = []
        self.columns = "*"
        self.start, self.end = 0, None
        self.sort_column = None
        self.on_conflict = None

    def select(self, columns):
        self.columns = columns
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def order(self, field):
        self.sort_column = field
        return self

    def range(self, start, end):
        self.start, self.end = start, end
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, *, on_conflict):
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict.split(",")
        return self

    def delete(self):
        self.op = "delete"
        return self

    def execute(self):
        service = self.client.service
        user = self.client.auth.user
        if user is None:
            raise RuntimeError("No Auth session")
        service.calls.append((self.op, self.name, deepcopy(self.payload), tuple(self.filters)))
        if self.op == "select" and service.fail_reads and self.name == "transactions":
            raise ConnectionError("Simulated remote read outage")
        if self.op in ("insert", "upsert") and service.fail_writes and self.name == "transactions":
            raise ConnectionError("Simulated remote write outage")
        owner_col = "id" if self.name == "profiles" else "user_id"

        def visible(row):
            return row.get(owner_col) == user.id and all(row.get(k) == v for k, v in self.filters)

        table = service.rows[self.name]
        matches = [row for row in table if visible(row)]
        if self.op == "select":
            if self.sort_column:
                matches.sort(key=lambda row: row[self.sort_column])
            matches = matches[self.start:self.end + 1 if self.end is not None else None]
            if self.columns != "*":
                names = self.columns.split(",")
                matches = [{k: row.get(k) for k in names} for row in matches]
            return SimpleNamespace(data=deepcopy(matches))
        if self.op in ("insert", "upsert"):
            row = deepcopy(self.payload)
            if row.get(owner_col) != user.id:
                raise PermissionError("RLS denied write for another user")
            if self.op == "upsert":
                existing = next((r for r in table if all(r.get(k) == row.get(k) for k in self.on_conflict)), None)
                if existing:
                    existing.update(row)
                    return SimpleNamespace(data=[deepcopy(existing)])
            if self.name != "profiles":
                row["id"] = service.next_id[self.name]
                service.next_id[self.name] += 1
            table.append(row)
            return SimpleNamespace(data=[deepcopy(row)])
        if self.op == "delete":
            service.rows[self.name] = [r for r in table if not visible(r)]
            return SimpleNamespace(data=deepcopy(matches))
        raise AssertionError(f"Unexpected fake operation: {self.op}")


class MemoryRpc:
    def __init__(self, client, name, params):
        self.client, self.name, self.params = client, name, params

    def execute(self):
        service = self.client.service
        user = self.client.auth.user
        if user is None:
            raise RuntimeError("No Auth session")
        service.calls.append(("rpc", self.name, deepcopy(self.params), user.id))
        uid = user.id
        params = self.params

        def owned(table, row_id):
            return next((r for r in service.rows[table] if r["id"] == row_id and r["user_id"] == uid), None)

        def add(table, row):
            row = deepcopy(row)
            row["user_id"] = uid
            row["id"] = service.next_id[table]
            service.next_id[table] += 1
            service.rows[table].append(row)
            return row

        if self.name == "create_user_goal":
            goal = add("goals", {"name": params["p_name"], "target": params["p_target"],
                                 "saved": params["p_saved"], "due_date": params["p_due_date"]})
            if params["p_saved"] > 0:
                add("transactions", {"txn_date": params["p_today"], "amount": params["p_saved"],
                                     "kind": "Expense", "category": "Savings contribution", "note": "Initial contribution",
                                     "goal_id": goal["id"]})
        elif self.name == "add_goal_contribution":
            goal = owned("goals", params["p_goal_id"])
            if not goal:
                raise PermissionError("Goal not owned")
            goal["saved"] += params["p_amount"]
            add("transactions", {"txn_date": params["p_txn_date"], "amount": params["p_amount"],
                                 "kind": "Expense", "category": "Savings contribution", "note": "Contribution",
                                 "goal_id": goal["id"]})
        elif self.name in ("update_user_transaction", "delete_user_transaction"):
            tx = owned("transactions", params["p_transaction_id"])
            if not tx:
                raise PermissionError("Transaction not owned")
            goal = owned("goals", tx.get("goal_id"))
            was_contribution = goal and tx["kind"] == "Expense" and tx["category"] == "Savings contribution"
            if self.name == "delete_user_transaction":
                if was_contribution:
                    goal["saved"] = max(0, goal["saved"] - tx["amount"])
                service.rows["transactions"].remove(tx)
            else:
                is_contribution = params["p_kind"] == "Expense" and params["p_category"] == "Savings contribution"
                if goal:
                    goal["saved"] = max(0, goal["saved"]
                        - (tx["amount"] if was_contribution else 0)
                        + (params["p_amount"] if is_contribution else 0))
                tx.update(txn_date=params["p_txn_date"], amount=params["p_amount"],
                          kind=params["p_kind"], category=params["p_category"], note=params["p_note"])
                if not is_contribution:
                    tx["goal_id"] = None
        elif self.name == "rename_user_category":
            old, new = params["p_old_name"], params["p_new_name"]
            for table, field in (("categories", "name"), ("transactions", "category"), ("budgets", "category")):
                for row in service.rows[table]:
                    if row["user_id"] == uid and row.get(field) == old:
                        row[field] = new
        elif self.name == "import_user_transactions":
            for row in params["p_rows"]:
                add("transactions", row)
        elif self.name == "restore_account_backup":
            # Fake only verifies routing/owner scoping. SQL rollback behavior
            # requires a live PostgreSQL integration test, not this mock.
            goal_map = {}
            for table in ("transactions", "budgets", "goals", "categories"):
                service.rows[table] = [r for r in service.rows[table] if r["user_id"] != uid]
            for row in params["p_goals"]:
                new_goal = add("goals", {k: v for k, v in row.items() if k != "id"})
                goal_map[row["id"]] = new_goal["id"]
            for row in params["p_transactions"]:
                row = deepcopy(row)
                if row["goal_id"] is not None:
                    row["goal_id"] = goal_map[row["goal_id"]]
                add("transactions", row)
            for row in params["p_budgets"]:
                add("budgets", row)
            for row in params["p_categories"]:
                add("categories", row)
            if params["p_currency"] is not None:
                profile = next((r for r in service.rows["profiles"] if r["id"] == uid), None)
                if profile:
                    profile["currency"] = params["p_currency"]
                else:
                    service.rows["profiles"].append({"id": uid, "currency": params["p_currency"]})
        elif self.name == "delete_my_account":
            for table in service.rows:
                key = "id" if table == "profiles" else "user_id"
                service.rows[table] = [r for r in service.rows[table] if r[key] != uid]
            del service.users[user.email]
        else:
            raise AssertionError(f"Unexpected RPC {self.name}")
        return SimpleNamespace(data=None)


class CloudAppTests(unittest.TestCase):
    def setUp(self):
        self.old_cwd = os.getcwd()
        self.workspace = tempfile.TemporaryDirectory()
        os.chdir(self.workspace.name)
        self.service = MemoryService()
        self.env = patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co", "SUPABASE_KEY": "test-anon-key",
        })
        self.client_patch = patch("supabase.create_client", side_effect=self.service.create_client)
        self.env.start()
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.env.stop()
        os.chdir(self.old_cwd)
        self.workspace.cleanup()

    def new_app(self):
        app = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def signup(self, app, email="alice@example.test"):
        app.text_input(key="su-email").set_value(email)
        app.text_input(key="su-pwd").set_value("test-password-123")
        app.button(key="FormSubmitter:signup_form-Create account").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["auth_mode"], "supabase")
        return app.session_state["user_id"]

    def sign_in(self, app, email):
        next(w for w in app.text_input if w.label == "Email address" and w.key is None).set_value(email)
        next(w for w in app.text_input if w.label == "Password" and w.key is None).set_value("test-password-123")
        app.button(key="FormSubmitter:login_form-Sign in").click().run()
        self.assertFalse(app.exception)

    def test_cloud_signup_income_persists_in_second_browser_without_sqlite_user(self):
        first = self.new_app()
        uid = self.signup(first)
        self.assertEqual(uid, str(uuid.uuid5(uuid.NAMESPACE_DNS, "alice@example.test")))
        first.radio(key="main-nav-radio").set_value("Transactions").run()
        first.number_input(key="add-inc-amt").set_value(148.0)
        first.text_input(key="add-inc-note").set_value("Remote income")
        first.button(key="FormSubmitter:add_income_form-Add income").click().run()
        self.assertFalse(first.exception)
        self.assertEqual(len(self.service.rows["transactions"]), 1)
        self.assertEqual(self.service.rows["transactions"][0]["user_id"], uid)
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)

        second = self.new_app()  # Fresh Streamlit session/client, same fake remote service.
        self.sign_in(second, "alice@example.test")
        self.assertEqual(second.session_state["user_id"], uid)
        second.radio(key="main-nav-radio").set_value("Transactions").run()
        self.assertFalse(second.exception)
        self.assertTrue(any("1 total transactions" in c.value for c in second.caption))
        self.assertEqual(len([call for call in self.service.calls if call[0] == "new-client"]), 2)

    def test_expense_budget_and_currency_survive_new_cloud_session(self):
        first = self.new_app()
        uid = self.signup(first)
        first.radio(key="main-nav-radio").set_value("Budgets & goals").run()
        next(w for w in first.selectbox if w.label == "Budget for").set_value("Food")
        next(w for w in first.number_input if w.label.startswith("Monthly limit")).set_value(2.0)
        first.button(key="FormSubmitter:budget_create_form-Save budget").click().run()
        self.assertFalse(first.exception)
        first.radio(key="main-nav-radio").set_value("Transactions").run()
        first.selectbox(key="add-exp-cat").set_value("Food")
        first.number_input(key="add-exp-amt").set_value(3.0)
        first.text_input(key="add-exp-note").set_value("Cloud expense")
        first.button(key="FormSubmitter:add_expense_form-Add expense").click().run()
        self.assertFalse(first.exception)
        self.assertEqual(len(self.service.rows["transactions"]), 1)
        self.assertEqual(self.service.rows["transactions"][0]["user_id"], uid)
        self.assertEqual(self.service.rows["budgets"][0]["category"], "Food")
        first.radio(key="main-nav-radio").set_value("Settings").run()
        next(w for w in first.selectbox if w.label == "Preferred display currency").set_value("USD · $").run()
        self.assertFalse(first.exception)
        self.assertEqual(self.service.rows["profiles"][0]["currency"], "$")
        second = self.new_app()
        self.sign_in(second, "alice@example.test")
        self.assertEqual(second.session_state["user_currency"], "$")
        second.radio(key="main-nav-radio").set_value("Transactions").run()
        self.assertTrue(any("1 total transactions" in c.value for c in second.caption))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM budgets").fetchone()[0], 0)

    def test_read_failure_does_not_show_or_write_local_transactions(self):
        app = self.new_app()
        uid = self.signup(app)
        with sqlite3.connect("pennywise.db") as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) "
                "VALUES (?, '2026-11-08', 10, 'Expense', 'Food', 'local secret', '2026-11-08')", (uid,),
            )
        self.service.fail_reads = True
        app.radio(key="main-nav-radio").set_value("Transactions").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Could not load transactions in Supabase" in e.value for e in app.error))
        self.assertFalse(any("local secret" in m.value for m in app.markdown))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0], 1)
        self.assertFalse(self.service.rows["transactions"])

    def test_remote_write_failure_never_falls_back_to_sqlite(self):
        app = self.new_app()
        self.signup(app)
        app.radio(key="main-nav-radio").set_value("Transactions").run()
        self.service.fail_writes = True
        app.number_input(key="add-inc-amt").set_value(5.0)
        app.button(key="FormSubmitter:add_income_form-Add income").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Could not save transaction in Supabase" in e.value for e in app.error))
        self.assertEqual(len(self.service.rows["transactions"]), 0)
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)

    def test_confirmation_required_does_not_create_sqlite_account(self):
        self.service.confirmation_required = True
        app = self.new_app()
        app.text_input(key="su-email").set_value("alice@example.test")
        app.text_input(key="su-pwd").set_value("test-password-123")
        app.button(key="FormSubmitter:signup_form-Create account").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state.get("user_id"))
        self.assertTrue(any("Check your email" in m.value for m in app.info))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_setting_category_budget_goal_and_account_deletion_stay_remote(self):
        app = self.new_app()
        uid = self.signup(app)
        app.radio(key="main-nav-radio").set_value("Settings").run()
        self.assertFalse(app.exception)
        next(w for w in app.text_input if w.label == "New category name").set_value("Pets")
        app.button(key="FormSubmitter:add_cat_form-Add category").click().run()
        self.assertFalse(app.exception)
        self.assertIn("Pets", [row["name"] for row in self.service.rows["categories"]])
        app.radio(key="main-nav-radio").set_value("Budgets & goals").run()
        next(w for w in app.number_input if w.label.startswith("Monthly limit")).set_value(400.0)
        app.button(key="FormSubmitter:budget_create_form-Save budget").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.service.rows["budgets"][0]["user_id"], uid)
        next(w for w in app.text_input if w.label == "Goal name").set_value("Rainy day")
        next(w for w in app.number_input if w.label.startswith("Already saved")).set_value(25.0)
        app.button(key="FormSubmitter:goal_create_form-Create savings goal").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.service.rows["goals"][0]["saved"], 25.0)
        self.assertEqual(self.service.rows["transactions"][0]["goal_id"], self.service.rows["goals"][0]["id"])
        app.radio(key="main-nav-radio").set_value("Settings").run()
        app.checkbox(key="confirm-delete-check").check().run()
        app.button(key="btn-delete-account").click().run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.session_state.get("user_id"))
        self.assertNotIn("alice@example.test", self.service.users)
        self.assertFalse(any(self.service.rows.values()))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_local_export_requires_verifiable_password_even_with_cloud_configured(self):
        # The old local account is not silently converted into a Supabase user.
        app = self.new_app()
        salt = "00" * 16
        digest = hashlib.pbkdf2_hmac("sha256", b"old-password", bytes.fromhex(salt), 1).hex()
        with sqlite3.connect("pennywise.db") as conn:
            uid = conn.execute(
                "INSERT INTO users (email, password_hash, salt, currency, created_at) "
                "VALUES (?, ?, ?, 'Rs', '2026-11-08')",
                ("old@example.test", f"pbkdf2_sha256$1${digest}", salt),
            ).lastrowid
            conn.execute(
                "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) "
                "VALUES (?, '2026-11-08', 100, 'Expense', 'Food', 'old data', '2026-11-08')", (str(uid),),
            )
        next(w for w in app.text_input if w.label == "Old local email").set_value("old@example.test")
        next(w for w in app.text_input if w.label == "Old local password").set_value("wrong")
        app.button(key="FormSubmitter:local-export-form-Verify and prepare backup").click().run()
        self.assertIsNone(app.session_state.get("_migration_uid"))
        next(w for w in app.text_input if w.label == "Old local password").set_value("old-password")
        app.button(key="FormSubmitter:local-export-form-Verify and prepare backup").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["_migration_uid"], str(uid))
        self.assertFalse(self.service.users)  # Cloud account has NOT been created yet.
        from app import local_backup_payload
        backup = local_backup_payload(str(uid), "old@example.test", "Rs")
        self.assertEqual(backup["transactions"][0]["note"], "old data")
        self.assertNotIn("password_hash", json.dumps(backup))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions").fetchone()[0], 1)

    def test_partial_configuration_blocks_local_signup_but_demo_stays_local(self):
        self.env.stop()
        self.env = patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_KEY": ""})
        self.env.start()
        app = self.new_app()
        app.text_input(key="su-email").set_value("new@example.test")
        app.text_input(key="su-pwd").set_value("test-password-123")
        app.button(key="FormSubmitter:signup_form-Create account").click().run()
        self.assertTrue(any("Finish configuring Supabase" in e.value for e in app.error))
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)
        app.button(key="demo-btn").click().run()
        self.assertEqual(app.session_state["auth_mode"], "demo")
        self.assertFalse(app.exception)


class LocalRestoreTests(unittest.TestCase):
    def test_local_restore_is_atomic_and_never_overwrites_another_user(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                from app import init_sqlite_db, restore_local_backup
                init_sqlite_db()
                with sqlite3.connect("pennywise.db") as conn:
                    uid = conn.execute(
                        "INSERT INTO users (email, password_hash, salt, currency, created_at) "
                        "VALUES ('mine@test.example', '', '', 'Rs', '2026-11-08')"
                    ).lastrowid
                    other_id = conn.execute(
                        "INSERT INTO users (email, password_hash, salt, currency, created_at) "
                        "VALUES ('other@test.example', '', '', '€', '2026-11-08')"
                    ).lastrowid
                    for owner, note in ((str(uid), "Keep me"), (str(other_id), "Other's record")):
                        conn.execute(
                            "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) "
                            "VALUES (?, '2026-11-08', 50, 'Expense', 'Food', ?, '2026-11-08')",
                            (owner, note),
                        )
                    conn.execute(
                        "CREATE TRIGGER block_restore BEFORE INSERT ON transactions "
                        "WHEN NEW.note = 'Blocked' BEGIN SELECT RAISE(ABORT, 'abort restore'); END"
                    )
                backup = {
                    "users": [{"id": other_id, "email": "untrusted", "currency": "£"}],
                    "goals": [{"id": 99, "name": "Trip", "target": 100, "saved": 25, "due_date": "2027-01-01"}],
                    "transactions": [{"txn_date": "2026-11-08", "amount": 25, "kind": "Expense",
                                      "category": "Savings contribution", "note": "Blocked", "goal_id": 99}],
                    "budgets": [{"month": "2026-11", "category": "Food", "amount": 80}],
                    "categories": [{"name": "Pets"}],
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    restore_local_backup(str(uid), backup)
                with sqlite3.connect("pennywise.db") as conn:
                    self.assertEqual(conn.execute(
                        "SELECT note FROM transactions WHERE user_id = ?", (str(uid),)
                    ).fetchall(), [("Keep me",)])
                    self.assertEqual(conn.execute("SELECT count(*) FROM goals").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT currency FROM users WHERE id = ?", (uid,)).fetchone()[0], "Rs")
                    conn.execute("DROP TRIGGER block_restore")
                backup["transactions"][0]["note"] = "Restored"
                self.assertEqual(restore_local_backup(str(uid), backup), "£")
                with sqlite3.connect("pennywise.db") as conn:
                    linked = conn.execute(
                        "SELECT note,goal_id,user_id FROM transactions WHERE user_id = ?", (str(uid),)
                    ).fetchone()
                    self.assertEqual(linked[0], "Restored")
                    self.assertEqual(linked[2], str(uid))
                    self.assertEqual(linked[1], conn.execute(
                        "SELECT id FROM goals WHERE user_id = ?", (str(uid),)
                    ).fetchone()[0])
                    self.assertEqual(conn.execute("SELECT currency FROM users WHERE id = ?", (uid,)).fetchone()[0], "£")
                    self.assertEqual(conn.execute(
                        "SELECT note FROM transactions WHERE user_id = ?", (str(other_id),)
                    ).fetchone()[0], "Other's record")
            finally:
                os.chdir(old_cwd)

    def test_local_account_deletion_never_removes_another_case_variant_email(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                from app import delete_user_account, init_sqlite_db
                init_sqlite_db()
                with sqlite3.connect("pennywise.db") as conn:
                    uid = conn.execute(
                        "INSERT INTO users (email,password_hash,salt,currency,created_at) "
                        "VALUES ('alex@example.test','','','Rs','2026-11-08')"
                    ).lastrowid
                    another = conn.execute(
                        "INSERT INTO users (email,password_hash,salt,currency,created_at) "
                        "VALUES ('ALEX@example.test','','','Rs','2026-11-08')"
                    ).lastrowid
                self.assertTrue(delete_user_account(str(uid)))
                with sqlite3.connect("pennywise.db") as conn:
                    self.assertEqual(conn.execute("SELECT id FROM users").fetchall(), [(another,)])
            finally:
                os.chdir(old_cwd)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.service = MemoryService()
        self.first = self.service.create_client("https://example.supabase.co", "test-anon-key")
        self.second = self.service.create_client("https://example.supabase.co", "test-anon-key")
        self.first.auth.sign_up({"email": "alice@example.test", "password": "pw"})
        self.second.auth.sign_up({"email": "bob@example.test", "password": "pw"})
        self.a = SupabaseStore(self.first, self.first.auth.user.id)
        self.b = SupabaseStore(self.second, self.second.auth.user.id)

    def test_store_refuses_mismatched_or_missing_auth_session(self):
        with self.assertRaises(RemoteStorageError):
            SupabaseStore(self.first, self.b.user_id)
        signed_out = self.service.create_client("https://example.supabase.co", "test-anon-key")
        with self.assertRaises(RemoteStorageError):
            SupabaseStore(signed_out, self.a.user_id)

    def test_owner_scoped_crud_and_goal_accounting(self):
        self.a.set_currency("₹")
        self.b.set_currency("€")
        self.a.add_category("Pets")
        self.a.add_transaction(date(2026, 11, 8), 45.0, "Expense", "Pets", "Kibble")
        self.b.add_transaction(date(2026, 11, 8), 120.0, "Income", "Salary")
        self.a.save_budget("2026-11", "Pets", 80)
        self.b.save_budget("2026-11", "Food", 150)
        self.a.create_goal("Laptop", 500, 10, date(2027, 1, 1), date(2026, 11, 8))
        goal = self.a.goals()[0]
        self.a.contribute_goal(goal["id"], 15, date(2026, 11, 8))
        self.assertEqual(self.a.goals()[0]["saved"], 25)
        tx = next(r for r in self.a.transactions() if r["goal_id"] == goal["id"] and r["amount"] == 15)
        self.a.edit_transaction(tx["id"], date(2026, 11, 9), 5, "Expense", "Savings contribution", "Edited")
        self.assertEqual(self.a.goals()[0]["saved"], 15)
        self.a.delete_transaction(tx["id"])
        self.assertEqual(self.a.goals()[0]["saved"], 10)
        self.a.rename_category("Pets", "Animals")
        self.assertEqual(self.a.categories(), ["Animals"])
        self.assertEqual(self.a.transactions()[0]["category"], "Animals")
        self.assertEqual(self.a.budgets()[0]["category"], "Animals")
        self.assertEqual(self.b.currency(), "€")
        self.assertEqual(len(self.b.transactions()), 1)
        self.assertEqual(self.b.transactions()[0]["category"], "Salary")
        self.assertEqual(self.b.budgets()[0]["category"], "Food")
        self.a.delete_account()
        self.assertEqual(len(self.b.transactions()), 1)
        self.assertEqual(self.b.currency(), "€")

    def test_existing_sqlite_backup_restores_to_signed_in_uuid_not_source_user_id(self):
        data = {
            "users": [{"id": 1, "email": "old@example.test", "currency": "£", "password_hash": "secret"}],
            "goals": [{"id": 8, "user_id": 1, "name": "Trip", "target": 100, "saved": 20, "due_date": "2027-01-01"}],
            "transactions": [{"id": 5, "user_id": 1, "txn_date": "2026-11-08", "amount": 20,
                              "kind": "Expense", "category": "Savings contribution", "note": "Saved", "goal_id": 8}],
            "budgets": [{"user_id": 1, "month": "2026-11", "category": "Food", "amount": 80}],
            "categories": [{"user_id": 1, "name": "Books"}],
        }
        self.assertEqual(self.a.restore_backup(data), "£")
        saved_tx = self.a.transactions()[0]
        self.assertEqual(saved_tx["goal_id"], self.a.goals()[0]["id"])
        self.assertEqual(self.a.currency(), "£")
        self.assertEqual(self.b.currency(), "Rs")
        self.assertTrue(all(r["user_id"] == self.a.user_id for r in self.service.rows["transactions"]))
        call = next(c for c in self.service.calls if c[0] == "rpc" and c[1] == "restore_account_backup")
        self.assertFalse(any("user_id" in row or "password_hash" in row
                             for key, rows in call[2].items() if isinstance(rows, list) for row in rows))

    def test_invalid_or_incomplete_backup_never_calls_destructive_rpc(self):
        empty = {k: [] for k in ("transactions", "budgets", "goals", "categories")}
        for data in (
            {}, {**empty, "transactions": [{"txn_date": "2026-11-08", "amount": -1,
                                              "kind": "Expense", "category": "Food"}]},
            {**empty, "transactions": [{"txn_date": "2026-11-08", "amount": 10,
                                              "kind": "Expense", "category": "Food", "goal_id": 50}]},
            {**empty, "users": [{"currency": "unsupported"}]},
            {**empty, "budgets": [{"month": "bad", "category": "Food", "amount": 10}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.a.restore_backup(data)
        self.assertFalse(any(c[0] == "rpc" for c in self.service.calls))
        self.assertEqual(validate_backup(empty)[0], empty)

    def test_paged_reads_and_remote_outage_raise_instead_of_silent_fallback(self):
        for i in range(1205):
            self.a.add_transaction(date(2026, 11, 8), 1, "Income", "Other", f"{i}")
        self.assertEqual(len(self.a.transactions()), 1205)
        ranges = [c for c in self.service.calls if c[0] == "select" and c[1] == "transactions"]
        self.assertEqual(len(ranges), 3)
        self.service.fail_reads = True
        with self.assertRaises(RemoteStorageError):
            self.a.transactions()
