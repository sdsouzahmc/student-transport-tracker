"""Office: parent notices outbox (in-app + e-mail), failure tracking and retry, delay / cancellation broadcasts."""
from datetime import date

import pandas as pd
import streamlit as st

import db
import engine as E
from ui import msg

user = st.session_state.user
st.title("✉️ Parent notices")
if "nt_last" in st.session_state:
    msg(*st.session_state.pop("nt_last"))
st.caption("Every accepted movement sends an in-app notice and an e-mail to each verified guardian. Failed e-mails can be retried; "
           "notices from scans captured offline are labelled as delayed.")

k = db.q1("""SELECT COUNT(*) n, SUM(status='FAILED') f, SUM(attempts>1) r, SUM(delayed_offline) d FROM notifications""")
m = st.columns(4)
m[0].metric("Notices", k["n"] or 0)
m[1].metric("Failed (current)", k["f"] or 0)
m[2].metric("Retried", k["r"] or 0)
m[3].metric("Delayed (offline)", k["d"] or 0)

c1, c2 = st.columns(2)
if c1.button(f"🔁 Retry failed e-mails ({k['f'] or 0})", disabled=not k["f"]):
    n, ok = E.retry_failed()
    db.audit(user["username"], user["role"], "NOTICE_RETRY", "notifications", "", f"{ok}/{n} delivered")
    st.session_state.nt_last = ("ok" if ok == n else "warn", f"Retried {n}: {ok} delivered, {n - ok} still failing (max 3 attempts, then call).")
    st.rerun()

with c2.popover("📣 Delay / cancellation notice", width="stretch"):
    d = st.date_input("Date", value=date.fromisoformat(db.today()), key="bc_d").isoformat()
    runs = db.q("SELECT r.id, r.trip_no, r.status, b.bus_no FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.date=? ORDER BY r.trip_no, b.bus_no", (d,))
    if not runs:
        st.caption("No trips on this date (start the trip first, or pick another date).")
    else:
        rid = st.selectbox("Trip", [r["id"] for r in runs], format_func=lambda i: next(f"{r['bus_no']} Trip {r['trip_no']} ({r['status']})" for r in runs if r["id"] == i))
        kind = st.radio("Type", ["DELAY", "CANCELLED"], horizontal=True)
        text = st.text_input("Message", value="Running about 20 minutes late due to traffic." if kind == "DELAY" else "This trip is cancelled today. Please collect your child.")
        if st.button("Send to all guardians on this trip", type="primary"):
            n = E.broadcast_run(rid, kind, text, user["username"], user["role"])
            st.session_state.nt_last = ("ok", f"Notice sent for {n} child(ren).")
            st.rerun()

f1, f2, f3 = st.columns(3)
status = f1.multiselect("Status", ["SENT", "FAILED"])
chan = f2.multiselect("Channel", ["IN_APP", "EMAIL"])
search = f3.text_input("Search text / student")
w, p = ["1=1"], []
if status:
    w.append(f"n.status IN ({','.join('?' * len(status))})")
    p += status
if chan:
    w.append(f"n.channel IN ({','.join('?' * len(chan))})")
    p += chan
if search:
    w.append("(n.message LIKE ? OR s.name LIKE ?)")
    p += [f"%{search}%"] * 2
rows = db.q(f"""SELECT n.ts AS Queued, n.channel AS Channel, n.kind AS Type, g.name AS Guardian, g.email AS "E-mail", s.name AS Student,
                n.message AS Message, n.status AS Status, n.attempts AS Attempts, n.last_error AS Error,
                CASE n.delayed_offline WHEN 1 THEN 'Delayed offline update' ELSE '' END AS Label, n.sent_ts AS Sent
                FROM notifications n LEFT JOIN guardians g ON g.id=n.guardian_id LEFT JOIN students s ON s.id=n.student_id
                WHERE {' AND '.join(w)} ORDER BY n.id DESC LIMIT 2000""", p)
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=460)
