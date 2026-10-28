from __future__ import annotations

import html, io, json, math, os, sqlite3, tempfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st
from postgrest.exceptions import APIError
from supabase import AuthApiError, create_client
import streamlit as st

# Inject custom CSS to hide the header bar and collapse the top padding
st.markdown(
    """
    <style>
    /* Hide the entire top header bar */
    [data-testid="stHeader"] {
        visibility: hidden;
        height: 0%;
    }
    
    /* Remove the default top padding to bring content up */
    .stAppViewBlockContainer {
        padding-top: 0rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

CATEGORIES=["Food","Transport","Utilities","Education","Shopping","Entertainment","Housing","Health","Salary","Freelance","Other","Savings contribution"]
CURRENCIES={"PKR · Rs":"Rs","USD · $":"$","EUR · €":"€","GBP · £":"£","INR · ₹":"₹","AED · د.إ":"د.إ"}
GREEN="#245f4d"

st.set_page_config(page_title="Pennywise · Personal Finance",page_icon="🌱",layout="wide",initial_sidebar_state="collapsed")

def get_client():
    if "supabase_client" not in st.session_state:
        try:
            if "supabase" in st.secrets:
                settings=st.secrets["supabase"]
                url=settings["url"]
                key=settings["anon_key"]
            else:
                url=st.secrets["SUPABASE_URL"]
                key=st.secrets["SUPABASE_KEY"]
        except (KeyError, FileNotFoundError) as exc:
            raise ValueError("Configure SUPABASE_URL and SUPABASE_KEY in .streamlit/secrets.toml, or use [supabase].url and [supabase].anon_key.") from exc
        if not str(url).strip() or not str(key).strip():
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must both have non-empty values.")
        st.session_state.supabase_client=create_client(str(url),str(key))
    return st.session_state.supabase_client

def signed_in_user_id():
    return st.session_state.user_id

def ensure_profile(user_id,default_currency="Rs"):
    if default_currency not in CURRENCIES.values():
        default_currency="Rs"
    client=get_client()
    existing=client.table("profiles").select("currency").eq("id",user_id).execute().data
    if existing:
        return existing[0]["currency"]
    client.table("profiles").upsert({"id":user_id,"currency":default_currency},on_conflict="id").execute()
    return default_currency

def show_schema_setup_error():
    st.error("The Pennywise tables are missing from this Supabase project's schema.")
    st.markdown("Open the Supabase **SQL Editor**, run the complete `supabase_schema.sql` file from this project, then retry. Make sure this app is configured with the same Supabase project.")

def account_rows(table,columns="*"):
    return get_client().table(table).select(columns).eq("user_id",signed_in_user_id()).execute().data

def money(x,s): return f"{s} {x:,.0f}"
def transactions(uid):
    rows=(get_client().table("transactions")
          .select("id,txn_date,amount,kind,category,note,goal_id")
          .eq("user_id",uid).order("txn_date",desc=True).order("id",desc=True).execute().data)
    d=pd.DataFrame(rows).rename(columns={"txn_date":"date","kind":"type","note":"description"})
    if not d.empty: d["date"]=pd.to_datetime(d.date).dt.date
    return d

def normalize_backup(data):
    if not isinstance(data,dict):
        raise ValueError("The backup must contain a JSON object.")
    users=data.get("users",[])
    if not isinstance(users,list) or any(not isinstance(user,dict) for user in users):
        raise ValueError("The backup account information is invalid.")
    normalized={}
    for table in ("transactions","budgets","goals","categories"):
        rows=data.get(table,[])
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise ValueError(f"The {table} backup data is invalid.")
        normalized[table]=rows
    transactions_data=[]
    for row in normalized["transactions"]:
        txn_date=date.fromisoformat(str(row["txn_date"])).isoformat()
        kind=str(row["kind"])
        amount=float(row["amount"])
        if kind not in ("Income","Expense") or not math.isfinite(amount) or amount<=0:
            raise ValueError("A transaction has an invalid type or amount.")
        category=str(row["category"]).strip()
        if not category: raise ValueError("A transaction category cannot be empty.")
        transactions_data.append({"id":row.get("id"),"txn_date":txn_date,"amount":amount,"kind":kind,
                                  "category":category,"note":str(row.get("note") or ""),
                                  "created_at":str(row.get("created_at") or datetime.now(timezone.utc).isoformat()),
                                  "goal_id":row.get("goal_id")})
    budgets_data=[]
    for row in normalized["budgets"]:
        month=str(row["month"])
        datetime.strptime(month,"%Y-%m")
        amount=float(row["amount"])
        if not math.isfinite(amount) or amount<0: raise ValueError("A budget amount must be a finite non-negative number.")
        budgets_data.append({"month":month,"category":str(row.get("category") or "All spending"),"amount":amount})
    goals_data=[]
    for row in normalized["goals"]:
        target=float(row["target"]); saved=float(row.get("saved") or 0)
        if not math.isfinite(target) or not math.isfinite(saved) or target<=0 or saved<0: raise ValueError("A savings goal has an invalid amount.")
        name=str(row["name"]).strip()
        if not name: raise ValueError("A savings goal name cannot be empty.")
        goals_data.append({"id":row.get("id"),"name":name,"target":target,
                           "saved":saved,"due_date":str(row.get("due_date") or "")})
    categories_data=[]
    for row in normalized["categories"]:
        name=str(row["name"]).strip()
        if not name: raise ValueError("A custom category name cannot be empty.")
        categories_data.append({"name":name})
    return {"transactions":transactions_data,"budgets":budgets_data,
            "goals":goals_data,"categories":categories_data}

def sqlite_backup(uploaded_file,email):
    with tempfile.NamedTemporaryFile(delete=False,suffix=".db") as temporary:
        temporary.write(uploaded_file.getvalue())
        temp_path=temporary.name
    try:
        connection=sqlite3.connect(temp_path)
        connection.row_factory=sqlite3.Row
        try:
            available={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" not in available:
                raise ValueError("This SQLite file does not contain a Pennywise account.")
            user=connection.execute("SELECT id,email,currency FROM users WHERE lower(email)=lower(?)",(email,)).fetchone()
            if not user:
                raise ValueError("The SQLite backup has no account matching your signed-in email.")
            data={"users":[{"email":user["email"],"currency":user["currency"]}]}
            for table in ("transactions","budgets","goals","categories"):
                data[table]=[]
                if table in available:
                    data[table]=[dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE user_id=?",(user["id"],))]
            return data
        finally:
            connection.close()
    finally:
        os.remove(temp_path)

def restore_backup(data,uid):
    normalized=normalize_backup(data)
    users=data.get("users",[])
    get_client().rpc("restore_account_backup",{
        "p_transactions":normalized["transactions"],
        "p_budgets":normalized["budgets"],
        "p_goals":normalized["goals"],
        "p_categories":normalized["categories"],
    }).execute()
    if users and isinstance(users[0],dict):
        currency=users[0].get("currency")
        if currency in CURRENCIES.values():
            get_client().table("profiles").update({"currency":currency}).eq("id",uid).execute()

def start_demo_session():
    response=get_client().auth.sign_in_anonymously()
    user=response.user
    if not user:
        raise ValueError("Supabase did not return a demo account.")
    ensure_profile(user.id)
    get_client().rpc("seed_demo_data").execute()
    st.session_state.user_id=user.id
    st.session_state.user_email="Demo account"

def css():
    st.markdown("""<style>
    @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Source+Sans+3:wght@400;500;600;700&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0,0&display=swap');
    :root{--ink:#253b35;--green:#245f4d;--muted:#78877f;--paper:#f7f7f1;--line:#e8ebe5}
    html,body,[class*="css"]{font-family:'Source Sans 3',sans-serif;color:var(--ink)} .stApp{background:var(--paper)}
    .block-container{max-width:1240px;padding-top:.45rem;padding-bottom:3rem}
    h1,h2,h3,h4{font-family:'Playfair Display',serif!important;color:var(--ink)}
    [data-testid="stSidebar"],[data-testid="collapsedControl"]{display:none}
    [data-testid="stVerticalBlockBorderWrapper"]{background:transparent;border:0!important;box-shadow:none}
    [data-testid="stMetric"]{background:transparent;border:0;padding:10px 6px;border-bottom:1px solid var(--line)}
    [data-testid="stMetricLabel"]{color:var(--muted)} [data-testid="stMetricValue"]{color:var(--ink);font-size:1.5rem}
    .brand{font-family:'Playfair Display',serif;font-size:1.45rem;font-weight:700;color:#214c3e;display:flex;align-items:center;gap:8px}
    .brand-mark{font-family:'Material Symbols Rounded';font-size:21px;color:#d9e6a5;background:#245f4d;border-radius:50%;width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center}
    .topline{border-bottom:1px solid #e9ebe5;padding:5px 0 9px;margin-bottom:8px}
    .hero{background:#dcece2;border-radius:26px;padding:28px 32px;margin:10px 0 18px;display:flex;justify-content:space-between;align-items:center;gap:20px}
    .hero-kicker{color:#507564;text-transform:uppercase;letter-spacing:.1em;font-size:.72rem;font-weight:700}
    .hero h2{font-size:2rem;margin:8px 0;color:#233e35}.hero p{color:#61776d;margin:0}
    .hero-value{font-size:2.5rem;letter-spacing:-.04em}.hero-box{background:#ffffffbd;border-radius:20px;padding:17px 20px;min-width:215px}
    .moment{background:#edf3d9;border-radius:18px;padding:19px;color:#52675c;line-height:1.55}
    .txn-icon{width:34px;height:34px;border-radius:12px;background:#e6f1e9;color:#245f4d;display:flex;align-items:center;justify-content:center;font-family:'Material Symbols Rounded';font-size:20px}
    .txn-meta{font-size:.82rem;color:#809087}.txn-row-rule{height:1px;background:#e9eee9;margin:6px 0 6px 44px}
    button[data-testid^="stBaseButton-primary"]{background:#245f4d!important;color:white!important;border:0!important;border-radius:12px!important;min-height:42px}
    .stButton button[data-testid="stBaseButton-secondary"]{border:1px solid var(--line)!important;border-radius:12px!important;color:var(--green)!important}
    [role="radiogroup"]{gap:.3rem;background:white;border:1px solid var(--line);border-radius:15px;padding:5px;width:fit-content;margin:6px auto 18px;max-width:100%}
    [role="radiogroup"] label{border-radius:10px;padding:6px 10px!important;color:#587067!important;white-space:nowrap}
    [role="radiogroup"] label:has(input:checked){background:#e5f0e8;color:#245f4d!important;font-weight:700}
    [role="radiogroup"] label>div>div:first-child{display:none!important}
    [data-testid="stPlotlyChart"]{background:transparent}
    @media(max-width:700px){
      .block-container{padding-left:14px!important;padding-right:14px!important;padding-top:.35rem!important}
      .hero{display:block;padding:20px;border-radius:20px}.hero-box{margin-top:16px;min-width:0}.hero-value{font-size:2rem}.hero h2{font-size:1.65rem}
      /* Give the brand its own line; group month and sign-out without stacking controls. */
      [data-testid="stHorizontalBlock"]:has(.brand){display:grid!important;grid-template-columns:minmax(0,1fr) auto!important;align-items:center!important;gap:5px 8px!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]{width:auto!important;min-width:0!important;flex:none!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:first-child{grid-column:1/-1}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:nth-child(3){display:none!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:nth-child(4) [data-testid="stHorizontalBlock"]{display:flex!important;flex-wrap:nowrap!important;align-items:center!important;justify-content:flex-end!important;gap:5px!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:nth-child(4) [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:first-child{flex:0 0 34px!important;width:34px!important;min-width:34px!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:nth-child(4) [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:last-child{flex:0 0 auto!important;width:auto!important;min-width:0!important}
      [data-testid="stHorizontalBlock"]:has(.brand)>[data-testid="stColumn"]:nth-child(4) button{white-space:nowrap!important;padding:.3rem .5rem!important;font-size:.8rem!important}
      [role="radiogroup"]{width:100%;overflow-x:auto;justify-content:flex-start;flex-wrap:nowrap;scrollbar-width:none}
      [role="radiogroup"] label{flex:0 0 auto;font-size:.82rem!important;padding:6px 8px!important}
      [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]){display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px!important}
      [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])>[data-testid="stColumn"]{width:auto!important;min-width:0!important;flex:none!important}
      [class*="st-key-txn-row-"] [data-testid="stHorizontalBlock"]{display:flex!important;flex-wrap:nowrap!important;gap:4px!important;align-items:center!important}
      [class*="st-key-txn-row-"] [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:nth-child(1){flex:0 0 34px!important;width:34px!important;min-width:34px!important}
      [class*="st-key-txn-row-"] [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:nth-child(2){flex:1 1 auto!important;width:auto!important;min-width:0!important}
      [class*="st-key-txn-row-"] [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:nth-child(3){flex:0 0 auto!important;min-width:68px!important;width:auto!important}
      [class*="st-key-txn-row-"] [data-testid="stHorizontalBlock"]>[data-testid="stColumn"]:nth-child(n+4){flex:0 0 28px!important;width:28px!important;min-width:28px!important}
      [data-testid="stMetricValue"]{font-size:1.2rem!important;overflow-wrap:anywhere}
    }
    </style>""",unsafe_allow_html=True)

def auth():
    css(); left,right=st.columns([1,1],gap="large")
    with left:
        st.markdown('<div class="brand"><span class="brand-mark">eco</span>Pennywise</div>',unsafe_allow_html=True)
        st.markdown('<div class="hero"><div><div class="hero-kicker">YOUR MONEY, MADE SIMPLE</div><h2>Small check-ins.<br/>More confidence.</h2><p>Understand your habits and shape the month ahead.</p></div></div>',unsafe_allow_html=True)
        st.caption("Sign in securely with your Supabase account. Your records are stored in the cloud.")
    with right:
        st.markdown("### Welcome")
        login,signup=st.tabs(["Sign in","Create account"])
        with login:
            with st.form("login"):
                email=st.text_input("Email address"); password=st.text_input("Password",type="password"); go=st.form_submit_button("Sign in",type="primary",use_container_width=True)
            if go:
                try:
                    response=get_client().auth.sign_in_with_password({"email":email.strip(),"password":password})
                    st.session_state.user_id=response.user.id
                    st.session_state.user_email=response.user.email or email.strip()
                    metadata=response.user.user_metadata or {}
                    ensure_profile(response.user.id,metadata.get("currency","Rs"))
                    st.rerun()
                except AuthApiError:
                    st.error("Email or password is incorrect. If email confirmation is enabled, verify your email first.")
            with st.expander("Explore the demo account"):
                st.caption("Try Pennywise with sample transactions, budgets, and a savings goal. Your demo data is private to this session.")
                if st.button("Start demo",key="demo-login",type="primary",use_container_width=True):
                    try:
                        start_demo_session()
                        st.rerun()
                    except (AuthApiError,APIError,ValueError) as exc:
                        if isinstance(exc,APIError) and exc.code=="PGRST205":
                            show_schema_setup_error()
                        elif isinstance(exc,AuthApiError) and "anonymous sign-ins are disabled" in exc.message.lower():
                            st.error("Could not start the demo because anonymous sign-ins are disabled. In Supabase, enable **Allow anonymous sign-ins** under **Authentication → Sign In / Providers → Anonymous**, then retry.")
                        else:
                            st.error(f"Could not start the demo: {exc}")
        with signup:
            with st.form("signup"):
                email=st.text_input("Email address",key="signup-email"); password=st.text_input("Password",type="password",key="signup-password",help="Use at least 8 characters."); currency=st.selectbox("Currency",list(CURRENCIES),key="signup-currency"); go=st.form_submit_button("Create account",type="primary",use_container_width=True)
            if go:
                if len(password)<8: st.error("Choose a password with at least 8 characters.")
                else:
                    try:
                        response=get_client().auth.sign_up({"email":email.strip(),"password":password,"options":{"data":{"currency":CURRENCIES[currency]}}})
                        if response.session and response.user:
                            st.session_state.user_id=response.user.id
                            st.session_state.user_email=response.user.email or email.strip()
                            ensure_profile(response.user.id,CURRENCIES[currency])
                            st.rerun()
                        st.success("Account created. Check your inbox and spam folder for the confirmation email, then sign in. If it does not arrive, use the resend option below.")
                    except AuthApiError as exc:
                        st.error(f"Could not create the account: {exc.message}")
            with st.form("resend-confirmation"):
                resend_email=st.text_input("Email address for confirmation resend")
                resend=st.form_submit_button("Resend confirmation email")
            if resend:
                if not resend_email.strip():
                    st.error("Enter the email address you used to create your account.")
                else:
                    try:
                        get_client().auth.resend({"type":"signup","email":resend_email.strip()})
                        st.success("If this address has a pending confirmation, Supabase has sent another email. Check your inbox and spam folder.")
                    except AuthApiError as exc:
                        st.error(f"Could not resend the confirmation email: {exc.message}")
        st.info("Your account uses Supabase Auth. You can download or restore your own account data from Settings.")

def transaction_row(r,symbol,show_actions=True):
    icons={"Food":"restaurant","Transport":"directions_car","Utilities":"bolt","Education":"school","Shopping":"shopping_bag","Entertainment":"movie","Housing":"home","Health":"medical_services","Salary":"south_west","Freelance":"south_west","Savings contribution":"savings"}
    cols=st.columns([.55,4,1.35,.42,.42] if show_actions else [.55,4,1.6],vertical_alignment="center")
    cols[0].markdown(f"<div class='txn-icon'>{icons.get(r.category,'payments')}</div>",unsafe_allow_html=True)
    label="Savings contribution" if r.category=="Savings contribution" else (r.description or r.category)
    kind="Savings" if r.category=="Savings contribution" else r.type
    cols[1].markdown(f"**{html.escape(str(label))}**  \n<span class='txn-meta'>{r.date.strftime('%d %b %Y')} · {html.escape(str(r.category))} · {kind}</span>",unsafe_allow_html=True)
    sign="+" if r.type=="Income" else "−"; color="#2d785e" if r.type=="Income" else "#30443e"
    cols[2].markdown(f"<div style='text-align:right;color:{color};font-weight:600'>{sign}{money(r.amount,symbol)}</div>",unsafe_allow_html=True)
    if show_actions:
        if cols[3].button("✎",key=f"edit-{int(r.id)}",help="Edit transaction"): st.session_state.edit_transaction_id=int(r.id); st.rerun()
        if cols[4].button("♲",key=f"delete-{int(r.id)}",help="Delete transaction"):
            client=get_client(); uid=signed_in_user_id()
            if pd.notna(r.goal_id):
                goal=client.table("goals").select("saved").eq("id",int(r.goal_id)).eq("user_id",uid).execute().data
                if goal:
                    client.table("goals").update({"saved":max(0,float(goal[0]["saved"])-float(r.amount))}).eq("id",int(r.goal_id)).eq("user_id",uid).execute()
            client.table("transactions").delete().eq("id",int(r.id)).eq("user_id",uid).execute()
            st.toast("Transaction deleted."); st.rerun()

def add_transaction_form(uid,cats,symbol,kind):
    with st.form(f"add-{kind}",clear_on_submit=True):
        a,b,c=st.columns(3); dt=a.date_input("Date",value=date.today()); amount=b.number_input(f"Amount ({symbol})",min_value=.01,step=100.0); category=c.selectbox("Category",[x for x in cats if x not in ["Savings contribution","Salary","Freelance"]] if kind=="Expense" else [x for x in cats if x!="Savings contribution"])
        note=st.text_input("Description or notes",placeholder="e.g. Weekly groceries"); saved=st.form_submit_button(f"Add {kind.lower()}",type="primary",use_container_width=True)
    if saved:
        get_client().table("transactions").insert({"user_id":uid,"txn_date":dt.isoformat(),"amount":amount,"kind":kind,"category":category,"note":note,"created_at":datetime.now(timezone.utc).isoformat()}).execute()
        st.success(f"{kind} saved."); st.rerun()

def app():
    css(); uid=st.session_state.user_id
    symbol=ensure_profile(uid)
    month_options=[(date.today().replace(day=1)-pd.DateOffset(months=i)).date() for i in range(18)]
    brand,monthcol,bell,account=st.columns([3.2,2.5,.55,1.4],vertical_alignment="center")
    with brand: st.markdown('<div class="brand"><span class="brand-mark">eco</span>Pennywise</div>',unsafe_allow_html=True)
    with monthcol: month=st.selectbox("Month",month_options,format_func=lambda d:d.strftime("%B %Y"),label_visibility="collapsed",key="top-month")
    with bell:
        if st.button("♧",help="Notifications",use_container_width=True): st.toast("You're all caught up.")
    with account:
        av,signout=st.columns([.75,1.5],vertical_alignment="center"); av.markdown(f"<div style='background:#e7eee3;color:#245f4d;border-radius:50%;width:34px;height:34px;text-align:center;padding-top:6px;font-weight:700'>{html.escape(st.session_state.user_email[0].upper())}</div>",unsafe_allow_html=True)
        if signout.button("Sign out",key="signout"):
            get_client().auth.sign_out()
            st.session_state.clear()
            st.rerun()
    st.markdown('<div class="topline"></div>',unsafe_allow_html=True)
    page=st.radio("Your space",["Overview","Transactions","Budgets & goals","Import & export","Settings"],horizontal=True,label_visibility="collapsed",key="main-nav")
    df=transactions(uid)
    custom=sorted(row["name"] for row in account_rows("categories","name"))
    cats=sorted(set(CATEGORIES+custom))
    if page=="Overview":
        ym=month.strftime("%Y-%m"); d=df[df.date.apply(lambda x:x.strftime("%Y-%m")==ym)] if not df.empty else df
        inc=d.loc[d.type=="Income","amount"].sum() if not d.empty else 0; exp=d.loc[(d.type=="Expense")&(d.category!="Savings contribution"),"amount"].sum() if not d.empty else 0; net=inc-exp
        total_budgets=(get_client().table("budgets").select("amount").eq("user_id",uid).eq("month",ym).eq("category","All spending").execute().data)
        budget=sum(float(row["amount"]) for row in total_budgets)
        pct=min(100,exp/budget*100) if budget else 0
        st.markdown(f'<div class="hero"><div><div class="hero-kicker">YOUR MONEY, MADE SIMPLE · {month.strftime("%B %Y").upper()}</div><h2>Your spending this month</h2><div class="hero-value">{money(exp,symbol)}</div><p>{money(net,symbol)} net savings · '+("You're in control. Keep going at your own pace." if net>=0 else "Spending is above income this month. Review your plan below.")+f'</p></div><div class="hero-box"><div style="display:flex;justify-content:space-between"><span>Monthly budget used</span><b>{pct:.0f}%</b></div><div style="height:10px;background:#c3d8c9;border-radius:10px;margin:12px 0"><div style="height:10px;width:{pct}%;max-width:100%;background:#245f4d;border-radius:10px"></div></div><span>{money(max(budget-exp,0),symbol)} remaining</span></div></div>',unsafe_allow_html=True)
        if exp>inc and exp>0: st.warning(f"Expenses are {money(exp-inc,symbol)} higher than income this month. Review your recent spending and budget before adding more expenses.")
        if budget and exp>budget: st.warning(f"You've gone {money(exp-budget,symbol)} over your monthly budget.")
        elif budget and pct>=80: st.info(f"You've used {pct:.0f}% of your monthly budget.")
        today=date.today(); days_in_month=(date(today.year+(today.month==12),1 if today.month==12 else today.month+1,1)-timedelta(days=1)).day
        forecast=exp/today.day*days_in_month if ym==today.strftime("%Y-%m") and today.day else exp
        m1,m2,m3,m4=st.columns(4); m1.metric("Income",money(inc,symbol));m2.metric("Expenses",money(exp,symbol));m3.metric("Net savings",money(net,symbol));m4.metric("Month-end forecast",money(forecast,symbol))
        if not d.empty:
            ex=d[d.type=="Expense"].groupby("category",as_index=False).amount.sum().sort_values("amount",ascending=False)
            if not ex.empty:
                top=ex.iloc[0]; st.caption(f"Highest spending category: **{top.category}** · {money(top.amount,symbol)} ({top.amount/exp*100:.0f}% of spending)")
        st.markdown("### Spending over time")
        if not df.empty:
            hist=df[(df.date>=(date.today().replace(day=1)-pd.DateOffset(months=5)).date())&(df.category!="Savings contribution")].copy()
            if not hist.empty:
                hist["Month"]=pd.to_datetime(hist.date).dt.to_period("M").astype(str); hv=hist.groupby(["Month","type"],as_index=False).amount.sum()
                fig=px.bar(hv,x="Month",y="amount",color="type",barmode="group",color_discrete_map={"Expense":GREEN,"Income":"#e9a251"});fig.update_layout(height=300,margin=dict(l=5,r=5,t=8,b=5),plot_bgcolor="rgba(0,0,0,0)",paper_bgcolor="rgba(0,0,0,0)",legend=dict(orientation="h",y=1.12));st.plotly_chart(fig,use_container_width=True)
        c1,c2=st.columns([1.1,.9])
        with c1:
            st.markdown("### Where your money goes")
            if not d.empty:
                ex=d[(d.type=="Expense")&(d.category!="Savings contribution")].groupby("category",as_index=False).amount.sum()
                if not ex.empty:
                    pie=px.pie(ex,names="category",values="amount",hole=.68,color_discrete_sequence=[GREEN,"#e9a251","#91ad98","#dccda5","#8c87aa"]);pie.update_layout(height=310,margin=dict(l=5,r=5,t=5,b=5),paper_bgcolor="rgba(0,0,0,0)",legend=dict(orientation="h",y=-.05));st.plotly_chart(pie,use_container_width=True)
                    comp=px.bar(ex.sort_values("amount"),x="amount",y="category",orientation="h",color_discrete_sequence=[GREEN]);comp.update_layout(height=max(180,36*len(ex)),margin=dict(l=5,r=5,t=5,b=5),paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",showlegend=False);st.caption("Category comparison");st.plotly_chart(comp,use_container_width=True)
            else: st.caption("Your category breakdown will appear here.")
        with c2:
            st.markdown("### Category budget check")
            budgets=(get_client().table("budgets").select("category,amount").eq("user_id",uid).eq("month",ym).neq("category","All spending").execute().data)
            if budgets:
                for b in budgets:
                    used=d.loc[(d.type=="Expense")&(d.category==b["category"])&(d.category!="Savings contribution"),"amount"].sum() if not d.empty else 0;st.write(f"**{b['category']}** · {money(used,symbol)} / {money(b['amount'],symbol)}");st.progress(min(1,used/b["amount"]) if b["amount"] else 0)
            else: st.caption("Set category limits in Budgets & goals.")
        txcol,moment=st.columns([1.3,.7],gap="large")
        with txcol:
            st.markdown("### Recent transactions")
            if df.empty: st.caption("No saved transactions yet. Add one from Transactions or import a CSV.")
            else:
                for _,r in df.head(8).iterrows():
                    with st.container(key=f"txn-row-{int(r.id)}"):
                        transaction_row(r,symbol,False)
                    st.markdown('<div class="txn-row-rule"></div>',unsafe_allow_html=True)
        with moment:
            st.markdown("### A money moment")
            if inc and exp>inc: advice=f"Expenses are {money(exp-inc,symbol)} above income. Start with the highest expense category and check for recurring costs you can adjust."
            elif exp==0: advice="No expense transactions this month yet. Add entries as they happen and this check-in can highlight your patterns."
            elif not d[(d.type=="Expense")&(d.category!="Savings contribution")].empty:
                ex=d[(d.type=="Expense")&(d.category!="Savings contribution")].groupby("category").amount.sum().sort_values(ascending=False); cat=ex.index[0]; share=ex.iloc[0]/exp*100
                advice=f"{cat} is your largest spending category at {money(ex.iloc[0],symbol)} ({share:.0f}% of expenses). Scan those transactions for recurring costs or changes you want to plan for next month."
            else: advice="You're building a useful record. Check back after a few entries to spot patterns in your spending."
            st.markdown(f'<div class="moment">{html.escape(advice)}</div>',unsafe_allow_html=True)
    elif page=="Transactions":
        st.title("Transactions")
        add1,add2=st.columns(2)
        with add1:
            st.markdown("### Add income"); add_transaction_form(uid,[x for x in cats if x!="Savings contribution"],symbol,"Income")
        with add2:
            st.markdown("### Add expense"); add_transaction_form(uid,[x for x in cats if x not in ["Salary","Freelance","Savings contribution"]],symbol,"Expense")
        st.markdown("### Recent transactions")
        if df.empty: st.info("No transactions yet. Add an income or expense above.")
        else:
            f1,f2=st.columns(2); start=f1.date_input("From",value=date.today().replace(day=1),key="txn-from");end=f2.date_input("To",value=date.today(),key="txn-to")
            cat=st.selectbox("Category",["All categories"]+cats); filtered=df[(df.date>=start)&(df.date<=end)]
            if cat!="All categories": filtered=filtered[filtered.category==cat]
            st.caption(f"{len(filtered)} recorded entr{'y' if len(filtered)==1 else 'ies'}")
            edit_id=st.session_state.get("edit_transaction_id")
            if edit_id:
                row=df[df.id==edit_id]
                if not row.empty:
                    r=row.iloc[0];st.markdown("#### Edit transaction")
                    with st.form("edit-transaction"):
                        a,b,c=st.columns(3);nd=a.date_input("Date",value=r.date);na=b.number_input(f"Amount ({symbol})",min_value=.01,value=float(r.amount),step=100.0);nk=c.selectbox("Type",["Expense","Income"],index=["Expense","Income"].index(r.type));nc=st.selectbox("Category",cats,index=cats.index(r.category) if r.category in cats else 0);nn=st.text_input("Description or notes",value=r.description or "");save=st.form_submit_button("Save changes",type="primary")
                    if save:
                        client=get_client()
                        if pd.notna(r.goal_id) and r.category=="Savings contribution":
                            goal=client.table("goals").select("saved").eq("id",int(r.goal_id)).eq("user_id",uid).execute().data
                            if goal:
                                adjustment=(na-r.amount) if nc=="Savings contribution" else -r.amount
                                client.table("goals").update({"saved":max(0,float(goal[0]["saved"])+adjustment)}).eq("id",int(r.goal_id)).eq("user_id",uid).execute()
                        client.table("transactions").update({"txn_date":nd.isoformat(),"amount":na,"kind":nk,"category":nc,"note":nn,"goal_id":int(r.goal_id) if nc=="Savings contribution" and pd.notna(r.goal_id) else None}).eq("id",int(edit_id)).eq("user_id",uid).execute()
                        st.session_state.edit_transaction_id=None;st.toast("Changes saved.");st.rerun()
                    if st.button("Cancel editing"): st.session_state.edit_transaction_id=None;st.rerun()
            for _,r in filtered.iterrows():
                with st.container(key=f"txn-row-{int(r.id)}"): transaction_row(r,symbol,True)
                st.markdown('<div class="txn-row-rule"></div>',unsafe_allow_html=True)
    elif page=="Budgets & goals":
        st.title("Budgets & savings goals"); left,right=st.columns(2)
        with left:
            st.subheader("Monthly budgets"); bm=st.date_input("Budget month",value=month,key="budget-month"); ym=bm.strftime("%Y-%m")
            with st.form("budget-form"):
                bc=st.selectbox("Budget for",["All spending"]+[x for x in cats if x!="Savings contribution"]); ba=st.number_input(f"Monthly limit ({symbol})",min_value=0.0,step=1000.0); save=st.form_submit_button("Save budget",type="primary")
            if save:
                get_client().table("budgets").upsert({"user_id":uid,"month":ym,"category":bc,"amount":ba},on_conflict="user_id,month,category").execute()
                st.success("Budget saved.")
            budgets=(get_client().table("budgets").select("id,month,category,amount").eq("user_id",uid).order("month",desc=True).execute().data)
            for b in budgets:
                st.write(f"{b['month']} · {b['category']} · {money(b['amount'],symbol)}")
                if st.button("Delete budget",key=f"del-budget-{b['id']}"):
                    get_client().table("budgets").delete().eq("id",b["id"]).eq("user_id",uid).execute()
                    st.rerun()
        with right:
            st.subheader("Savings goals")
            with st.form("goal-form"):
                name=st.text_input("Goal name"); target=st.number_input(f"Target ({symbol})",min_value=1.0,step=1000.0); initial=st.number_input(f"Already saved ({symbol})",min_value=0.0,step=100.0); due=st.date_input("Target date",value=date.today()+timedelta(days=90)); save=st.form_submit_button("Create savings goal",type="primary")
            if save and name.strip():
                created=get_client().table("goals").insert({"user_id":uid,"name":name.strip(),"target":target,"saved":initial,"due_date":due.isoformat()}).execute().data
                if initial>0:
                    get_client().table("transactions").insert({"user_id":uid,"txn_date":date.today().isoformat(),"amount":initial,"kind":"Expense","category":"Savings contribution","note":f"Initial contribution · {name.strip()}","created_at":datetime.now(timezone.utc).isoformat(),"goal_id":created[0]["id"]}).execute()
                st.success("Savings goal created.");st.rerun()
            goals=account_rows("goals","id,name,target,saved,due_date")
            for g in goals:
                st.markdown(f"**{g['name']}** · {money(g['saved'],symbol)} of {money(g['target'],symbol)}");st.progress(min(1,g['saved']/g['target']) if g['target'] else 0)
                with st.form(f"contribute-{g['id']}"):
                    amount=st.number_input("Add to savings",min_value=.01,step=100.0,key=f"goal-amount-{g['id']}");when=st.date_input("Contribution date",value=date.today(),key=f"goal-date-{g['id']}");ok=st.form_submit_button("Add contribution")
                if ok:
                    get_client().table("goals").update({"saved":float(g["saved"])+amount}).eq("id",g["id"]).eq("user_id",uid).execute()
                    get_client().table("transactions").insert({"user_id":uid,"txn_date":when.isoformat(),"amount":amount,"kind":"Expense","category":"Savings contribution","note":f"Contribution · {g['name']}","created_at":datetime.now(timezone.utc).isoformat(),"goal_id":g["id"]}).execute()
                    st.success("Contribution added as a transaction.");st.rerun()
    elif page=="Import & export":
        st.title("Import & export"); st.subheader("Import bank transactions")
        uploaded=st.file_uploader("Upload a bank CSV",type=["csv"])
        if uploaded:
            try:
                raw=pd.read_csv(uploaded);raw.columns=[str(x).strip().lower().replace(" ","_") for x in raw.columns]
                date_col=next((x for x in ["date","transaction_date","txn_date","posted_date"] if x in raw),None); amount_col=next((x for x in ["amount","value"] if x in raw),None); desc_col=next((x for x in ["description","note","notes","details","merchant"] if x in raw),None); type_col=next((x for x in ["type","transaction_type"] if x in raw),None); cat_col="category" if "category" in raw else None
                debit=next((x for x in ["debit","withdrawal","money_out"] if x in raw),None);credit=next((x for x in ["credit","deposit","money_in"] if x in raw),None); good=[]
                if date_col and (amount_col or debit or credit):
                    for _,r in raw.iterrows():
                        try:
                            dt=pd.to_datetime(r[date_col]).date()
                            if amount_col: v=float(str(r[amount_col]).replace(",","").replace(symbol,"").replace("$","").strip());kind=str(r[type_col]).title() if type_col and pd.notna(r[type_col]) else ("Expense" if v<0 else "Income");v=abs(v)
                            else: de=float(r[debit] or 0) if debit and pd.notna(r[debit]) else 0;cr=float(r[credit] or 0) if credit and pd.notna(r[credit]) else 0;kind="Expense" if de else "Income";v=de or cr
                            if v>0: good.append((dt.isoformat(),v,kind if kind in ["Income","Expense"] else "Expense",str(r[cat_col]) if cat_col and pd.notna(r[cat_col]) else "Other",str(r[desc_col]) if desc_col and pd.notna(r[desc_col]) else "Imported from CSV"))
                        except (ValueError,TypeError): pass
                    st.write(f"Preview: **{len(good)}** valid rows of {len(raw)}")
                    if good and st.button("Import transactions",type="primary"):
                        records=[{"user_id":uid,"txn_date":row[0],"amount":row[1],"kind":row[2],"category":row[3],"note":row[4],"created_at":datetime.now(timezone.utc).isoformat()} for row in good]
                        get_client().table("transactions").insert(records).execute()
                        st.success(f"Imported {len(good)} transactions.");st.rerun()
                else: st.error("CSV must include a date column and an amount, debit, or credit column.")
            except Exception as e: st.error(f"Could not read CSV: {e}")
        st.subheader("Export")
        if df.empty: st.info("Add transactions to export your data.")
        else:
            start,end=st.date_input("Export date range",value=(date.today().replace(day=1),date.today()),key="export-range"); selected=df[(df.date>=start)&(df.date<=end)].copy(); st.download_button("Export selected transactions as CSV",selected.to_csv(index=False).encode("utf-8-sig"),file_name="pennywise-transactions.csv",mime="text/csv")
            try:
                xlsx=io.BytesIO(); selected.assign(date=selected.date.astype(str)).to_excel(xlsx,index=False,sheet_name="Transactions"); st.download_button("Export selected transactions as Excel",xlsx.getvalue(),file_name="pennywise-transactions.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            except ImportError: st.caption("Excel export needs the openpyxl package.")
            ym=month.strftime("%Y-%m"); md=df[df.date.apply(lambda x:x.strftime("%Y-%m")==ym)]; out=io.StringIO(); out.write(f"Month,{ym}\nIncome,{md.loc[md.type=='Income','amount'].sum() if not md.empty else 0}\nExpenses,{md.loc[md.type=='Expense','amount'].sum() if not md.empty else 0}\nNet savings,{(md.loc[md.type=='Income','amount'].sum()-md.loc[md.type=='Expense','amount'].sum()) if not md.empty else 0}\n"); st.download_button("Export monthly summary CSV",out.getvalue().encode("utf-8-sig"),file_name=f"pennywise-summary-{ym}.csv",mime="text/csv")
            try:
                from reportlab.lib import colors
                from reportlab.lib.pagesizes import letter
                from reportlab.platypus import SimpleDocTemplate,Table,TableStyle,Paragraph,Spacer
                from reportlab.lib.styles import getSampleStyleSheet
                pdf=io.BytesIO(); doc=SimpleDocTemplate(pdf,pagesize=letter); styles=getSampleStyleSheet(); inc_pdf=md.loc[md.type=="Income","amount"].sum() if not md.empty else 0; exp_pdf=md.loc[(md.type=="Expense")&(md.category!="Savings contribution"),"amount"].sum() if not md.empty else 0
                story=[Paragraph(f"Pennywise monthly summary · {month.strftime('%B %Y')}",styles["Title"]),Spacer(1,14),Paragraph(f"Income: {money(inc_pdf,symbol)} · Spending: {money(exp_pdf,symbol)} · Net savings: {money(inc_pdf-exp_pdf,symbol)}",styles["BodyText"]),Spacer(1,16)]
                rows=[["Date","Type","Category","Description","Amount"]]+[[str(r.date),r.type,r.category,str(r.description or ""),f"{symbol} {r.amount:,.2f}"] for _,r in md.iterrows()]
                table=Table(rows,repeatRows=1,colWidths=[68,60,90,205,78]);table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(GREEN)),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#dce5dd")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f3f7f3")]),("FONTSIZE",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"MIDDLE")]));story.append(table);doc.build(story);st.download_button("Download monthly summary as PDF",pdf.getvalue(),file_name=f"pennywise-summary-{ym}.pdf",mime="application/pdf")
            except ImportError: st.caption("PDF export needs the reportlab package.")
    elif page=="Settings":
        st.title("Settings & account data")
        cur_label=next((k for k,v in CURRENCIES.items() if v==symbol),"PKR · Rs"); new=st.selectbox("Display currency",list(CURRENCIES),index=list(CURRENCIES).index(cur_label))
        if CURRENCIES[new]!=symbol:
            get_client().table("profiles").update({"currency":CURRENCIES[new]}).eq("id",uid).execute()
            st.success("Currency updated.");st.rerun()
        st.markdown("### Custom category")
        with st.form("custom-category"):
            category=st.text_input("Category name");save=st.form_submit_button("Add category")
        if save and category.strip():
            try:
                get_client().table("categories").insert({"user_id":uid,"name":category.strip()}).execute()
                st.success("Category added.")
            except APIError as exc:
                if exc.code=="23505": st.error("That category already exists.")
                else: st.error(f"Could not add category: {exc.message}")
        st.markdown("### Backup and restore")
        payload={"users":[{"email":st.session_state.user_email,"currency":symbol}]}
        payload["transactions"]=account_rows("transactions","id,txn_date,amount,kind,category,note,created_at,goal_id")
        payload["budgets"]=account_rows("budgets","id,month,category,amount")
        payload["goals"]=account_rows("goals","id,name,target,saved,due_date")
        payload["categories"]=account_rows("categories","id,name")
        st.download_button("Download account backup",json.dumps(payload,indent=2).encode(),file_name="pennywise-backup.json",mime="application/json",type="primary")
        restore=st.file_uploader("Restore account backup (JSON or SQLite)",type=["json","db","sqlite","sqlite3"])
        if restore and st.button("Restore backup",key="restore-backup"):
            try:
                if restore.name.lower().endswith((".db",".sqlite",".sqlite3")):
                    data=sqlite_backup(restore,st.session_state.user_email)
                else:
                    data=json.loads(restore.getvalue())
                restore_backup(data,uid)
                st.success("Backup restored to your Supabase account.");st.rerun()
            except (APIError,ValueError,KeyError,TypeError,sqlite3.Error,json.JSONDecodeError) as exc:
                st.error(f"Could not restore backup: {exc}")
        st.caption("Restoring replaces your current transactions, budgets, goals, and custom categories. SQLite imports include only the account matching your signed-in email; passwords are never imported.")

def main():
    try:
        get_client()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    try:
        if "user_id" not in st.session_state: auth()
        else: app()
    except APIError as exc:
        if exc.code=="PGRST205":
            show_schema_setup_error()
            st.stop()
        raise

if __name__=="__main__": main()
