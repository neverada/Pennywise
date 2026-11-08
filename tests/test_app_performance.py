"""Regressions for expensive reruns and accidental demo reseeding."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class AppPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.app = AppTest.from_file(str(APP_PATH), default_timeout=30)
        self.app.run()  # Creates the empty SQLite database.
        self.assertFalse(self.app.exception)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def sign_in_test_user(self):
        with sqlite3.connect("pennywise.db") as conn:
            uid = conn.execute("INSERT INTO users (email) VALUES ('test@example.com')").lastrowid
        self.app.session_state["user_id"] = str(uid)
        self.app.session_state["user_email"] = "test@example.com"
        self.app.session_state["user_currency"] = "Rs"
        return str(uid)

    def test_existing_user_with_fewer_than_100_transactions_is_not_reseeded(self):
        uid = self.sign_in_test_user()
        with sqlite3.connect("pennywise.db") as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) "
                "VALUES (?, ?, 50, 'Expense', 'Food', 'Keep me', ?)",
                (uid, date.today().isoformat(), date.today().isoformat()),
            )
        self.app.run()
        self.app.radio(key="main-nav-radio").set_value("Transactions")
        self.app.run()
        self.assertFalse(self.app.exception)
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM transactions WHERE user_id = ? AND note = 'Keep me'", (uid,)).fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0], 1)

    def test_signup_starts_with_an_empty_ledger(self):
        self.app.text_input(key="su-email").set_value("new@example.com")
        self.app.text_input(key="su-pwd").set_value("password123")
        self.app.button(key="FormSubmitter:signup_form-Create account").click().run()
        self.assertFalse(self.app.exception)
        with sqlite3.connect("pennywise.db") as conn:
            uid = conn.execute("SELECT id FROM users WHERE email = 'new@example.com'").fetchone()[0]
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (str(uid),)).fetchone()[0], 0)

    def test_instant_demo_still_seeds_on_request(self):
        self.app.button(key="demo-btn").click().run()
        self.assertFalse(self.app.exception)
        uid = self.app.session_state["user_id"]
        with sqlite3.connect("pennywise.db") as conn:
            self.assertGreaterEqual(
                conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0],
                100,
            )

    def test_transactions_page_renders_one_page_and_resets_on_filter_change(self):
        uid = self.sign_in_test_user()
        with sqlite3.connect("pennywise.db") as conn:
            conn.executemany(
                "INSERT INTO transactions (user_id, txn_date, amount, kind, category, note, created_at) "
                "VALUES (?, ?, 50, 'Expense', 'Food', 'Sample', ?)",
                [(uid, date.today().isoformat(), date.today().isoformat())] * 80,
            )
        self.app.run()
        self.app.radio(key="main-nav-radio").set_value("Transactions")
        self.app.run()
        self.assertFalse(self.app.exception)
        edits = [b.key for b in self.app.button if b.key and b.key.startswith("edit-")]
        self.assertEqual(len(edits), 25)
        self.assertEqual(self.app.number_input(key="ledger-page").value, 1)

        self.app.number_input(key="ledger-page").set_value(2).run()
        self.assertEqual(self.app.session_state["ledger-page"], 2)
        self.assertNotEqual([b.key for b in self.app.button if b.key and b.key.startswith("edit-")][0], edits[0])

        self.app.text_input(key="filter-search").set_value("not found").run()
        self.assertFalse(self.app.exception)
        self.assertFalse([b for b in self.app.button if b.key and b.key.startswith("edit-")])
        self.assertEqual(self.app.session_state["ledger-page"], 1)


if __name__ == "__main__":
    unittest.main()
