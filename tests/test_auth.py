"""Account creation and sign-in against both shipped and freshly created SQLite files."""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def open_app(self, bundled=False):
        if bundled:
            shutil.copyfile(ROOT / "pennywise.db", "pennywise.db")
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def signup(self, app, email="new@example.test", password="test-password-123"):
        app.text_input(key="su-email").set_value(email)
        app.text_input(key="su-pwd").set_value(password)
        app.button(key="FormSubmitter:signup_form-Create account").click().run()
        self.assertFalse(app.exception)
        return app

    def login(self, app, email, password):
        next(w for w in app.text_input if w.label == "Email address" and w.key is None).set_value(email)
        next(w for w in app.text_input if w.label == "Password" and w.key is None).set_value(password)
        app.button(key="FormSubmitter:login_form-Sign in").click().run()
        self.assertFalse(app.exception)
        return app

    def test_signup_on_bundled_database_and_password_check(self):
        app = self.signup(self.open_app(bundled=True))
        uid = app.session_state["user_id"]
        with sqlite3.connect("pennywise.db") as conn:
            user = conn.execute(
                "SELECT id, email, currency, password_hash, salt, created_at FROM users WHERE id = ?", (uid,)
            ).fetchone()
            self.assertEqual(user[1:3], ("new@example.test", "Rs"))
            self.assertTrue(user[3].startswith("pbkdf2_sha256$"))
            self.assertEqual(len(user[4]), 32)
            self.assertTrue(user[5])
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0], 0)
        app.button(key="top-signout").click().run()
        self.login(app, "new@example.test", "wrong-password")
        self.assertIsNone(app.session_state.get("user_id"))
        self.assertIn("Incorrect email or password.", [e.value for e in app.error])
        self.login(app, "new@example.test", "test-password-123")
        self.assertEqual(app.session_state["user_id"], uid)
        self.assertFalse(app.session_state["_legacy_login"])

    def test_duplicate_signup_does_not_overwrite_account(self):
        app = self.open_app(bundled=True)
        with sqlite3.connect("pennywise.db") as conn:
            before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        self.signup(app, email="DEMO@pennywise.app")
        self.assertIsNone(app.session_state.get("user_id"))
        self.assertIn("An account with this email already exists. Sign in instead.", [e.value for e in app.error])
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], before)
            self.assertEqual(conn.execute("SELECT currency FROM users WHERE id = 1").fetchone()[0], "Rs")

    def test_fresh_database_and_old_minimal_users_schema(self):
        # An older installation could already have a users table without credential columns.
        with sqlite3.connect("pennywise.db") as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, currency TEXT NOT NULL DEFAULT 'Rs')")
            conn.execute("INSERT INTO users(email, currency) VALUES ('legacy@example.test', 'Rs')")
        app = self.signup(self.open_app())
        self.assertIsNotNone(app.session_state.get("user_id"))
        with sqlite3.connect("pennywise.db") as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            self.assertTrue({"password_hash", "salt", "created_at"} <= columns)
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT email FROM users WHERE id = 1").fetchone()[0], "legacy@example.test")

    def test_invalid_signup_and_unknown_signin_do_not_create_users(self):
        app = self.open_app()
        self.signup(app, password="short")
        self.assertIn("Password must be at least 8 characters.", [e.value for e in app.error])
        self.login(app, "absent@example.test", "any-password")
        self.assertIn("Account not found. Create an account or start the instant demo.", [e.value for e in app.error])
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_instant_demo_creates_account_with_shipped_schema(self):
        app = self.open_app(bundled=True)
        # Prove the demo creation path works if the bundled demo user is missing.
        with sqlite3.connect("pennywise.db") as conn:
            conn.execute("DELETE FROM users WHERE id = 1")
        app.button(key="demo-btn").click().run()
        self.assertFalse(app.exception)
        uid = app.session_state["user_id"]
        with sqlite3.connect("pennywise.db") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM transactions WHERE user_id = ?", (uid,)).fetchone()[0], 116)
            self.assertTrue(conn.execute("SELECT password_hash FROM users WHERE id = ?", (uid,)).fetchone()[0])

    def test_unverifiable_legacy_password_cannot_expose_account_backup(self):
        app = self.open_app(bundled=True)
        self.login(app, "demo@pennywise.app", "legacy-password-not-verified")
        self.assertIsNone(app.session_state.get("user_id"))
        self.assertTrue(any("cannot be verified" in e.value for e in app.error))
        # The explicit, clearly labelled shared demo remains available.
        app.button(key="demo-btn").click().run()
        self.assertEqual(app.session_state["auth_mode"], "demo")
        self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
