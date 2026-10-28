# Pennywise Personal Finance

A Streamlit personal finance dashboard with custom styling, structured tabs, financial tracking, budgeting, savings goals, CSV imports, visual analytics, and full account management.

## Features

- **Structured Layout**:
  - **Overview**: Month selector, monthly spending hero card, 4 key metrics (Income, Expenses, Net Savings, Month-end forecast), overspending alerts, Plotly historical cash flow bar chart, category donut breakdown, category budget tracking progress bars, recent transactions, and dynamic "A money moment" insight.
  - **Transactions**: Dedicated "Add income" and "Add expense" forms with custom category creation, date range filter, category filter, inline transaction editing, and deletion (with automatic savings-goal balance recalculation).
  - **Budgets & Goals**: Department and total monthly budget limits, savings goal tracking with deadlines, and milestone contributions that generate ledger records.
  - **Import & Export**: Bank statement CSV importer with automatic column detection and preview. Ledger exports as CSV and styled PDF summary reports (using ReportLab).
  - **Settings & Account Management**: Display currency switcher (`Rs`, `$`, `€`, `£`, `₹`, `د.إ`), custom category adding and renaming, JSON backup download and restore, and **Permanent Account Deletion**.
- **Permanent Account Deletion**:
  - Dedicated "Danger Zone" in Settings.
  - Secure confirmation to permanently wipe all transactions, budgets, goals, custom categories, and user profile data.

    
 ## Requirements & Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```
