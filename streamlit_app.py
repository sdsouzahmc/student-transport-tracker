"""Student Transport Tracker v2.0 — school bus tracking with student location registration and dynamic trip planning (crew app, office portal, parent portal; English / Arabic)."""
import os
import random

import streamlit as st

import db
from i18n import rtl_css, t

st.set_page_config(page_title="Student Transport Tracker", page_icon="🚌", layout="wide", initial_sidebar_state="auto")

BASE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(BASE, "School_Bus_Trip_Dataset.xlsx")


@st.cache_resource
def _boot():
    """First run: load the supplied dataset workbook (or generate the full-size demo if it is missing)."""
    fresh = not os.path.exists(db.DB_PATH)
    db.init_db(seed_data=False)
    if fresh or not db.q1("SELECT id FROM buses LIMIT 1"):
        if os.path.exists(DATASET):
            from dataset_import import import_workbook
            import_workbook(DATASET, os.path.basename(DATASET))
        else:
            db.init_db(reset=True)
    # one-time tidy of older data: production-style names
    db.x("UPDATE staff SET name=REPLACE(name,' Demo','') WHERE name LIKE '% Demo%'")
    db.x("UPDATE staff SET name='Dispatcher' WHERE name='Dispatcher Demo'")
    db.x("UPDATE users SET display_name=REPLACE(display_name,' Demo','') WHERE display_name LIKE '% Demo%'")
    db.x("UPDATE users SET display_name='Dispatcher' WHERE display_name='Dispatcher Demo'")
    db.x("UPDATE stops SET name=REPLACE(name,' demo','') WHERE name LIKE '% demo%'")
    db.x("UPDATE trip_runs SET sweep_by=REPLACE(sweep_by,' Demo','') WHERE sweep_by LIKE '% Demo%'")
    db.x("UPDATE manifest SET received_by=REPLACE(received_by,' Demo','') WHERE received_by LIKE '% Demo%'")
    import planner             # v2.0 tables (student locations, route plans) + initial enrolment locations
    planner.ensure_schema()
    planner.seed_locations()
    planner.sync_student_stops()   # badge / daily-list stops follow the approved locations
    if "demo" in (db.setting("school_name", "") or "").lower():
        db.set_setting("school_name", db.SCHOOL_NAME)
    return True


_boot()
if db.setting("v2_stop_sync") != "1":      # one-time on an already running server: align student stops with approved locations
    import planner as _pl
    _pl.sync_student_stops()
    db.set_setting("v2_stop_sync", "1")

st.markdown("""<style>
  :root { --navy:#1B2559; --indigo:#3D4FD6; --indigo-soft:#E9ECFC; --ink:#1A2036; --muted:#6B7390; --line:#E3E7F0;
          --green:#2E9E5B; --orange:#E07A1F; --red:#D93025; }
  .block-container {padding-top: 4.2rem !important; max-width: 1400px;}
  header[data-testid="stHeader"] {background: rgba(244,246,250,.92); backdrop-filter: blur(4px);}
  h1, h2, h3 {color: var(--ink); letter-spacing: -0.01em;}
  h1 {font-weight: 800 !important;}
  /* white rounded cards for bordered containers */
  div[data-testid="stVerticalBlockBorderWrapper"], div[data-testid="stContainer"][data-border="true"] {
      background:#FFFFFF; border:1px solid var(--line) !important; border-radius:14px !important;
      box-shadow: 0 1px 2px rgba(16,24,64,.04), 0 2px 8px rgba(16,24,64,.04); }
  div[data-testid="stExpander"] details {background:#FFFFFF; border-radius:12px; border:1px solid var(--line);}
  div[data-testid="stMetric"] {background:#FFFFFF; border:1px solid var(--line); border-radius:14px; padding:12px 16px;
      box-shadow: 0 1px 2px rgba(16,24,64,.04);}
  div[data-testid="stMetricValue"] {font-weight:800; color:var(--ink);}
  div[data-testid="stDataFrame"] {border-radius:12px; overflow:hidden;}
  /* sidebar */
  section[data-testid="stSidebar"] {background: var(--navy);}
  section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] {border-radius:10px; margin:2px 6px; padding:8px 12px;}
  section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"][aria-current="page"] {background: var(--indigo) !important;}
  section[data-testid="stSidebar"] a[data-testid="stSidebarNavLink"] span {color:#E8ECFF !important; font-weight:500;}
  .brand {font-size:1.7rem; font-weight:800; color:#FFFFFF; letter-spacing:-0.02em; margin:0 0 2px 4px;}
  .brand b {color:#8EA2FF;}
  .brand-sub {color:#AEB8E8; font-size:.8rem; margin:-6px 0 12px 2px;}
  [data-testid='stSidebarHeader'] img, [data-testid='stLogo'] {height:2.4rem !important; max-width:220px;}
  .nowrap{white-space:nowrap}
  .kpi .lab{font-size:.8rem;line-height:1.2;min-height:2.1em}
  .bc .top{flex-wrap:wrap}
  /* status messages */
  .ok   {background:#E8F6EE;border-left:6px solid var(--green);padding:10px 14px;border-radius:10px;font-size:1.02rem;margin:6px 0}
  .warn {background:#FFF3E6;border-left:6px solid var(--orange);padding:10px 14px;border-radius:10px;font-size:1.02rem;margin:6px 0}
  .error{background:#FDECEC;border-left:6px solid var(--red);padding:10px 14px;border-radius:10px;font-size:1.02rem;margin:6px 0}
  .card {background:#FFFFFF;border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin:6px 0}
  .muted{color:var(--muted);font-size:.85rem}
  .pill {display:inline-block;padding:2px 10px;border-radius:12px;font-size:.78rem;font-weight:600;background:var(--indigo-soft);color:var(--indigo)}
  .pill.green{background:#E3F4EA;color:var(--green)} .pill.blue{background:var(--indigo);color:#fff}
  .pill.grey{background:#EEF0F5;color:#5B6378} .pill.orange{background:#FDEBD8;color:var(--orange)} .pill.red{background:#FDE3E1;color:var(--red)}
  .badge-sample{background:#FDE3E1;color:#C62828;font-weight:700;font-size:.75rem;padding:6px 12px;border-radius:8px;letter-spacing:.04em}
  /* KPI cards */
  .kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px;margin:8px 0 16px}
  @media (max-width: 1100px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr));}}
  @media (max-width: 640px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));}}
  .kpi{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:0 1px 2px rgba(16,24,64,.04)}
  .kpi .lab{display:flex;align-items:center;gap:8px;color:#3A4160;font-size:.86rem;font-weight:500}
  .kpi .val{font-size:2.1rem;font-weight:800;color:var(--ink);line-height:1.15;margin-top:4px}
  .kpi .val.orange{color:var(--orange)} .kpi .sub{color:var(--muted);font-size:.75rem}
  .kpi svg{width:20px;height:20px;flex:none}
  .sect{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--ink);font-size:1.02rem;margin:2px 0 8px}
  .sect svg{width:18px;height:18px}
  .bar{display:flex;align-items:center;gap:10px;margin:10px 0}
  .bar .name{width:44%;font-size:.88rem;color:#2A3150}
  .bar .track{flex:1;height:22px;background:transparent}
  .bar .fill{height:22px;border-radius:3px}
  .bar .num{width:36px;text-align:right;font-weight:800}
  .safety{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .safety .lab{color:var(--muted);font-size:.8rem} .safety .v{font-size:1.5rem;font-weight:800;display:flex;align-items:center;gap:8px}
  .att{display:flex;justify-content:space-between;align-items:center;background:#F7F8FC;border-radius:10px;padding:8px 12px;margin:6px 0;font-size:.88rem}
  .att .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--orange);margin-right:8px}
  /* live trip cards */
  .buscards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
  @media (max-width: 1100px){.buscards{grid-template-columns:repeat(2,minmax(0,1fr));}}
  @media (max-width: 640px){.buscards{grid-template-columns:1fr;}}
  .bc{background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:0 1px 2px rgba(16,24,64,.04)}
  .bc .top{display:flex;align-items:center;gap:8px} .bc .no{font-weight:800;font-size:1.05rem}
  .bc .area{border:1px solid var(--line);border-radius:8px;padding:1px 8px;font-size:.75rem;color:#4A5272}
  .bc .prog{position:relative;height:6px;background:#DCE3FB;border-radius:4px;margin:16px 0 4px}
  .bc .prog .f{height:6px;background:#9DB0F7;border-radius:4px}
  .bc .prog .k{position:absolute;top:-5px;width:16px;height:16px;border-radius:50%;background:var(--indigo);border:3px solid #fff;box-shadow:0 0 0 1px #c9d2f5}
  .bc .ends{display:flex;justify-content:space-between;font-size:.72rem;color:var(--muted)}
  .bc .grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;margin-top:10px}
  .bc .l{font-size:.72rem;color:var(--muted)} .bc .v{font-size:.86rem;font-weight:600;color:var(--ink)}
  .crewcnt{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:6px 0 10px}
  .crewcnt div{background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px 10px;text-align:center}
  .crewcnt b{display:block;font-size:1.6rem;line-height:1.1} .crewcnt span{font-size:.72rem;color:var(--muted)}
  @media (max-width: 640px){ h1{font-size:1.7rem !important} h3{font-size:1.15rem !important} .block-container{padding-left:.8rem;padding-right:.8rem} }
  div[data-testid='stPopover'] button[kind='primary']{background:#D93025 !important;border-color:#D93025 !important;color:#fff !important;font-weight:700}
  .datebar{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:10px;padding:6px 12px;margin:-4px 0 10px;font-size:1rem;color:var(--ink)}
  .datebar span{color:var(--muted);font-size:.8rem}
  [data-testid='stToolbarActions']{display:none !important}
  /* touch-friendly buttons */
  div[data-testid='stPills'] button, div[data-testid='stButtonGroup'] button{min-height:44px;padding:8px 14px;font-size:.98rem}
  div[data-testid='stButton'] button{min-height:44px}
  .bc.flat{border:none;box-shadow:none;padding:2px 2px 6px} .bc.closed .prog .f{background:#9ED3B0} .bc.closed .prog .k{background:var(--green)}
""" + rtl_css(), unsafe_allow_html=True)


def login():
    st.markdown("<div style='font-size:2.2rem;font-weight:800;letter-spacing:-0.02em;color:#1B2559;line-height:1.3'>Auto<span style='color:#3D4FD6'>Trace</span></div>"
                f"<div style='font-size:1.25rem;font-weight:700;margin-top:-4px'>{t('Student Transport Tracker')} <span style=\'font-size:.8rem;color:#6B7390;font-weight:600\'>v2.0</span></div>", unsafe_allow_html=True)
    st.caption(db.setting("school_name", db.SCHOOL_NAME))
    c1, c2 = st.columns([1, 1])
    with c1:
        lang = st.radio(t("Language"), ["English", "العربية"], horizontal=True, index=1 if st.session_state.get("lang") == "ar" else 0)
        if (lang == "العربية") != (st.session_state.get("lang") == "ar"):
            st.session_state.lang = "ar" if lang == "العربية" else "en"
            st.rerun()
        pend = st.session_state.get("mfa_pending")
        if pend:
            st.info(f"Two-step verification for **{pend['display_name']}**. Enter the 6-digit code sent to your registered mobile "
                    f"(**{st.session_state.mfa_code}**).")
            with st.form("mfa"):
                code = st.text_input(t("Verification code"), max_chars=6)
                ok = st.form_submit_button(t("Verify"), type="primary")
            if ok:
                if code.strip() == st.session_state.mfa_code:
                    st.session_state.user = pend
                    st.session_state.pop("mfa_pending")
                    db.audit(pend["username"], pend["role"], "LOGIN", "user", pend["username"], "password + MFA")
                    st.rerun()
                else:
                    db.audit(pend["username"], pend["role"], "MFA_FAILED", "user", pend["username"])
                    st.error(t("Wrong code."))
            if st.button("Cancel"):
                st.session_state.pop("mfa_pending")
                st.rerun()
            return
        with st.form("login"):
            u = st.text_input(t("Username"))
            p = st.text_input(t("PIN / password"), type="password")
            ok = st.form_submit_button(t("Sign in"), type="primary")
        if ok:
            user = db.get_user(u, p)
            if not user:
                db.audit(u, "", "LOGIN_FAILED", "user", u)
                st.error(t("Wrong username or PIN."))
            elif user["role"] in db.MFA_ROLES:
                st.session_state.mfa_pending = user
                st.session_state.mfa_code = f"{random.randint(0, 999999):06d}"
                st.rerun()
            else:
                st.session_state.user = user
                db.audit(user["username"], user["role"], "LOGIN", "user", user["username"])
                st.rerun()
    with c2:
        st.markdown("<div class='card' style='margin-top:2.2rem'><b>Student transport</b><br><span class='muted'>Live bus tracking, scan-in / scan-out, school handover and parent notifications.</span></div>", unsafe_allow_html=True)


if "user" not in st.session_state:
    login()
    st.stop()

user = st.session_state.user
role = user["role"]

st.logo(os.path.join(BASE, "autotrace_logo.svg"), size="large", icon_image=os.path.join(BASE, "autotrace_icon.svg"))
with st.sidebar:
    st.markdown("<div class='brand-sub'>Student Transport Tracker v2.0 · " + db.setting("school_name", db.SCHOOL_NAME) + "</div>", unsafe_allow_html=True)
    st.markdown(f"**{user['display_name']}**  \n<span class='muted'>{role.title()} · {user['username']}</span>", unsafe_allow_html=True)
    lang = st.radio(t("Language"), ["English", "العربية"], horizontal=True, index=1 if st.session_state.get("lang") == "ar" else 0)
    if (lang == "العربية") != (st.session_state.get("lang") == "ar"):
        st.session_state.lang = "ar" if lang == "العربية" else "en"
        st.rerun()
    if st.button(t("Sign out"), width="stretch"):
        db.audit(user["username"], role, "LOGOUT", "user", user["username"])
        for k in list(st.session_state):
            if k != "lang":
                del st.session_state[k]
        st.rerun()

P = {
    "dash": st.Page("page_dashboard.py", title="Overview", icon=":material/home:"),
    "crew": st.Page("page_crew.py", title=t("Crew app"), icon=":material/qr_code_scanner:"),
    "receipt": st.Page("page_receipt.py", title=t("School receipt"), icon=":material/school:"),
    "parent": st.Page("page_parent.py", title=t("My children"), icon=":material/family_restroom:"),
    "live": st.Page("page_live.py", title="Live trips", icon=":material/directions_bus:"),
    "lists": st.Page("page_lists.py", title="Daily bus lists", icon=":material/groups:"),
    "exceptions": st.Page("page_exceptions.py", title="Incidents & approvals", icon=":material/warning:"),
    "notices": st.Page("page_notices.py", title="Parent notices", icon=":material/mail:"),
    "reports": st.Page("page_reports.py", title="Reports", icon=":material/bar_chart:"),
    "setup": st.Page("page_setup.py", title="Setup & data", icon=":material/settings:"),
    "plan": st.Page("page_planning.py", title="Trip planning", icon=":material/route:"),
    "locs": st.Page("page_locations.py", title="Student locations", icon=":material/home_pin:"),
}
MENU = {
    "supervisor": ["crew"], "caretaker": ["crew"], "driver": ["crew"],
    "receiving": ["receipt"],
    "guardian": ["parent"],
    "dispatcher": ["dash", "live", "plan", "lists", "locs", "exceptions", "notices", "reports", "crew", "receipt"],
    "manager": ["dash", "live", "plan", "lists", "locs", "exceptions", "notices", "reports", "setup", "crew", "receipt"],
    "admin": ["dash", "live", "plan", "lists", "locs", "exceptions", "notices", "reports", "setup", "crew", "receipt", "parent"],
}
st.navigation([P[k] for k in MENU.get(role, [])]).run()
