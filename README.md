# Pennywise Personal Finance

A Streamlit app for tracking income and expenses, monthly budgets, savings goals, CSV imports, reports, and JSON backups. **Supabase accounts use Supabase Auth and PostgreSQL storage for all account data.** Supabase Free provides persistent cloud storage and authentication, but do not mistake that for an accessible, automatic backup: keep your own private JSON exports or off-site database dumps. The instant demo is a **separate, shared, server-local SQLite workspace**, not a cloud account or a backup.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Without Supabase settings, newly created accounts and the demo use `pennywise.db` on this server only. Local storage is **not** cross-device backup: two browsers seeing the same server's SQLite file does not mean that the data is stored remotely. Treat any server-local database as ephemeral on redeploy/restart, particularly on hosted Streamlit services. The repository's bundled `pennywise.db` is **sample data, not a copy of a hosted user's account**. Do not replace or delete a production database during setup.

The **Start instant demo** button resets the shared demo account to **116 transactions dated 2026-08-01 through 2026-10-28, spanning three months**, plus sample budgets, goals, and categories. Do not put private information in the shared demo. The demo remains local even if Supabase is configured; it is prominently labelled in the app.

## Enable Supabase accounts

1. **Before changing an existing deployment**, ask each local-account owner to sign in to the old app and download **Settings → Download account backup (JSON)**. Keep a private copy of the current hosted SQLite database as well if you administer that server. Existing SQLite records are **not automatically migrated or deleted** by the code change. If a previous app is already gone, do not assume the bundled `pennywise.db` contains its data.
2. Create a Supabase project. In its **SQL Editor**, run the entire [`supabase_schema.sql`](supabase_schema.sql) as a privileged project administrator. It creates `profiles`, `transactions`, `budgets`, `goals`, and `categories`; owner-scoped row-level-security policies; and authenticated RPCs for atomic goal edits, imports, restore, category rename, and account deletion. Rerunning the SQL does not erase account rows (the backup restore RPC replaces rows *only* when a signed-in user explicitly invokes it). If your existing tables have an incompatible schema, inspect and migrate them first rather than dropping them.
3. Supply the project's URL and **publishable/anon** key to Streamlit. For Streamlit Community Cloud, use the app's **Secrets** settings; for local development, put the following in `.streamlit/secrets.toml` (ignored by Git):

   ```toml
   [supabase]
   url = "https://YOUR_PROJECT.supabase.co"
   anon_key = "YOUR_ANON_OR_PUBLISHABLE_KEY"
   ```

   Alternatively set `SUPABASE_URL` and `SUPABASE_ANON_KEY` (or `SUPABASE_KEY`) as environment variables. **Never put a service-role/secret key in the app or repository**; that key bypasses row-level security. An incomplete configuration disables account sign-in and sign-up rather than creating more local accounts. The local demo still works.
4. Start/redeploy the app, create an account through **Create account** (Supabase Auth), and follow your project's email-confirmation settings. If confirmation is required, confirm the email and then sign in. New accounts start empty; the app creates a `profiles` row for the authenticated UUID when the session is established. A previously local email/password does **not** become a Supabase login automatically.
5. Verify with your actual Supabase project before relying on it: sign in from a second browser/device, add a transaction and budget, sign out/in and restart the app, and compare the records in the dashboard with Supabase Table Editor. Inspect the project's RLS policies and test that another user cannot read/write the first user's rows. **These live checks have not been performed in this repository**; offline/mock tests alone do not prove hosted behavior or backup status.

### Migrate a local account's JSON backup

- Keep the original JSON in a private location; it contains financial history. If the old app is still available, download its account backup **before** redeployment. The cloud login screen does not export server-local accounts. If the old app is gone and you do not have a JSON backup, the data may no longer be recoverable; the bundled `pennywise.db` is sample data, not a copy of a hosted user's account.
- In the signed-in Supabase account, go to **Settings → Backup and restore**, download a backup of any current cloud data first, upload the old JSON, review the transaction/budget/goal/category counts, and explicitly confirm **Replace account records from JSON**. Restore **replaces** that account's four datasets in one PostgreSQL transaction; it does not change the Supabase Auth email/password or import source user IDs. Linked goal IDs are remapped to new owner-scoped IDs, and a supported currency is restored with the data. Malformed/incomplete JSON is rejected before deletion. Keep the original JSON until you verify every imported record in Supabase and on a second device.
- The local demo has its own SQLite backup/restore path. Its data is shared and can be reset; it is never a substitute for a real account.

## What is stored where

| Signed-in mode | Identity | Account data | Persistence |
| --- | --- | --- | --- |
| Supabase configured | Supabase Auth UUID | `public.profiles`, `transactions`, `budgets`, `goals`, `categories` in Supabase PostgreSQL; RLS and signed-in RPCs | Project-managed, visible to the same Auth account after sign-in on another device; still make separate backups |
| No Supabase configuration | Verified SQLite user ID | Local `pennywise.db` | Only this server's disk; **not** a durable hosted backup |
| Instant demo | Labelled shared local SQLite demo ID | Local `pennywise.db` sample records | Resettable, **not private or synced** |

The Streamlit app keeps an authenticated Supabase client in each browser session, not a global client shared across users. Reads and writes fail visibly if Supabase is unavailable; they **never silently fall back to SQLite** for a Supabase account. Passwords are handled by Supabase Auth (or PBKDF2 for explicitly local accounts) and are not included in JSON exports. Account deletion invokes an authenticated, self-only database function that removes the Supabase Auth user and cascades to owned rows; a failed RPC is reported, not claimed as success.

## Tests and limitations

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile app.py supabase_store.py run_app.py
npm install --no-package-lock                  # optional SQL integration test dependency
npm run test:schema
```

The Python tests cover local signup/demo, independent Streamlit sessions using an **in-memory fake** of Auth/PostgREST, owner-scoped remote calls, remote read/write failure without SQLite fallback, JSON validation/goal remapping, and SQLite restore rollback. The optional `test:schema` executes the **actual** `supabase_schema.sql` against a local PGlite PostgreSQL engine with simulated Auth users/roles; it exercises RLS isolation, RPC goal accounting, restore rollback, and self-only Auth deletion. It is stronger than a mock, but **not a hosted Supabase integration test**. Supabase's real Auth, project privileges, email-confirmation flow, and persistence after app redeployment still require testing against **your Supabase project**. No credentials or hosted environment are bundled with this repo. This checkout was exercised with Python 3.11.2, Streamlit 1.67.0, supabase-py 2.34.1, pandas 3.1.1, Plotly 7.1.0, and PGlite 0.5.15/PostgreSQL 18.6 (WASM); `requirements.txt` expresses supported minimums rather than a lockfile.
