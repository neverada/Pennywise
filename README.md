# PennyWise - Personal Expense & Budget Dashboard

PennyWise is a modern personal finance tracking dashboard built with Python and Streamlit. It helps users manage their income, track expenses, set category budgets, monitor savings goals, and visualize cash flow trends across multiple months.

---

## 🌟 Features

* **Transaction Ledger:** Record income and expense entries with date, category, amount, and custom notes.
* **Budget Monitoring:** Set overall spending limits and category-specific monthly budgets with visual alert indicators.
* **Savings Goal Manager:** Create savings targets and log direct contributions into the ledger.
* **Interactive Analytics:** Multi-month cash flow trends (bar/line charts) and category breakdown graphs using Plotly.
* **Import & Export:** Batch import bank statements via CSV, export ledger summaries in CSV, and generate dynamic PDF reports with ReportLab.
* **Dual Storage System:** 
  * **Cloud Mode:** Supabase integration using PostgreSQL with Row-Level Security (RLS) for multi-user isolation.
  * **Local / Demo Mode:** Fast local SQLite database (`pennywise.db`) for offline use or instant demo data exploration.
* **Account Backup:** Export and restore full account data in JSON format.

---

## 🛠️ Tech Stack & Requirements

* **Language:** Python 3.11+
* **Web Framework:** [Streamlit](https://streamlit.io/) (`>= 1.35.0`)
* **Data Processing:** [Pandas](https://pandas.pydata.org/) (`>= 2.0.0`)
* **Data Visualization:** [Plotly](https://plotly.com/python/) (`>= 5.18.0`)
* **PDF Report Generation:** [ReportLab](https://www.reportlab.com/) (`>= 4.0.0`)
* **Database Options:**
  * Local: **SQLite** (Python standard library)
  * Cloud (Optional): **Supabase** (`supabase >= 2.0.0`, `postgrest >= 0.10.0`)

---

## 🚀 Getting Started & Local Setup

Follow these steps to run PennyWise locally on your machine.

### 1. Prerequisites

Ensure you have **Python 3.11** or higher installed on your system. You can check your Python version by running:

```bash
python --version
```

### 2. Clone the Repository

```bash
git clone https://github.com/neverada/Pennywise.git
cd Pennywise
```

### 3. Create a Virtual Environment (Recommended)

* **macOS / Linux:**
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```
* **Windows (Command Prompt):**
  ```cmd
  python -m venv venv
  venv\Scripts\activate
  ```

### 4. Install Dependencies

Install all required packages using `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 5. Run the Application

You can launch the Streamlit app using either of the following commands:

**Option A: Standard Streamlit Run**
```bash
streamlit run app.py
```

**Option B: Using the Included Launcher Script**
```bash
python run_app.py
```

Open your browser and navigate to `http://localhost:8501` (or `http://localhost:3000` if launched via `run_app.py`).

---

## ⚙️ Configuration (Optional Supabase Setup)

PennyWise works out of the box using local SQLite storage (`pennywise.db`). To enable Supabase cloud authentication and cloud database features:

1. Create a project at [Supabase](https://supabase.com/).
2. Run the SQL initialization script provided in `supabase_schema.sql` inside your Supabase SQL Editor to create tables and set up Row-Level Security (RLS).
3. Create a `.streamlit/secrets.toml` file in the project root:

```toml
SUPABASE_URL = "https://your-supabase-project.supabase.co"
SUPABASE_KEY = "your-supabase-anon-key"
```

If these keys are present, the app automatically switches to cloud mode.

---

## 🧪 Running Tests

To run the automated test suite for authentication, schema integration, and performance:

```bash
pytest tests/
```

---

