# Pennywise personal finance app

A Streamlit personal finance dashboard inspired by the supplied Pennywise design. It supports Supabase email/password accounts, per-user records protected by Supabase Row Level Security, income and expense tracking, budgets, savings goals, charts, forecasts, CSV bank imports, CSV exports, PDF summaries, custom categories, and JSON backups.

Adding an initial amount to a new savings goal or recording a later contribution also creates a dated **Savings contribution** entry in the transaction history. These transfers remain visible and exportable, while staying out of spending and category totals. Editing or deleting a linked contribution also adjusts the goal balance.

## Run it

1. Install Python 3.10 or newer.
2. Create a Supabase project and run [`supabase_schema.sql`](./supabase_schema.sql) in the Supabase SQL Editor. This creates the app tables, Row Level Security policies, and the atomic account-backup restore function.
3. Configure Streamlit secrets locally in `.streamlit/secrets.toml` (the directory name starts with a dot) or in the app's Streamlit Cloud settings. The app accepts either format:

   ```toml
   SUPABASE_URL = "https://YOUR_PROJECT_REF.supabase.co"
   SUPABASE_KEY = "YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY"
   ```

   Or:

   ```toml
   [supabase]
   url = "https://YOUR_PROJECT_REF.supabase.co"
   anon_key = "YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY"
   ```

   Use the project's public anon/publishable key, **never** its service-role key. Supabase Auth must have email/password enabled. If email confirmation is enabled, new users need to confirm their address before signing in.
4. Install the packages: `python -m pip install -r requirements.txt`
5. Start the app: `streamlit run app.py`, then create an account or sign in.

The app does not create or manage the Supabase schema automatically; apply the SQL file before first use. All table operations use the signed-in Supabase client, and the database policies independently enforce ownership using `auth.uid()`.

## Bank CSV format

The importer accepts columns for date, amount, type, category, and description. It also recognizes common bank headers such as `transaction_date`, `details`, `debit`, `credit`, `withdrawal`, and `deposit`. Amounts below zero are treated as expenses. Review your bank's CSV before importing; imports create new transactions and are not deduplicated automatically.

## Import existing local data

Create a Supabase account with the same email address as the local Pennywise account, then sign in and open **Settings → Backup and restore**. Upload the existing `pennywise.db` SQLite file to import only records belonging to that matching email. Existing JSON backups can also be restored there. Restore replaces the signed-in account's transactions, budgets, goals, and custom categories; it maps old savings-goal IDs to their new database IDs. Password hashes are never imported: create a new Supabase Auth password, since local SQLite passwords cannot be transferred to Supabase Auth.

The Settings page provides JSON backup download and restore. Supabase handles email confirmation and password storage. Keep backups secure, and use a trusted Supabase project; this app is a personal finance tracker, not a substitute for a professionally secured financial service.
