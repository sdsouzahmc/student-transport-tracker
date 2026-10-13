"""School receiving staff: confirm receipt of arriving children (spec §5 school receipt)."""
import uuid

import streamlit as st

import db
import engine as E
from db import SCHOOL_STOP
from i18n import t
from ui import gps_fix, lines_table, msg

user = st.session_state.user
st.title("🏫 " + t("School receipt"))
me = db.q1("SELECT * FROM staff WHERE id=?", (user["staff_id"],))
receivers = [r["name"] for r in db.q("SELECT name FROM staff WHERE role='receiving' ORDER BY name")]
receiver = me["name"] if me and me["role"] == "receiving" else st.selectbox("Receiving staff member", receivers)
st.caption(f"Receiving as **{receiver}**. Children are checked out at school only when a named staff member accepts them.")

if "rc_last" in st.session_state:
    for lv, m in st.session_state.pop("rc_last"):
        msg(lv, m)

runs = db.q("""SELECT r.*, b.bus_no FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.status IN ('IN_PROGRESS','PENDING_SYNC')
               ORDER BY r.start_ts""")
waiting = []
for r in runs:
    for ln in E.run_lines(r["id"]):
        if ln["status"] == "ONBOARD":
            waiting.append(dict(ln, bus_no=r["bus_no"], trip_no=r["trip_no"]))
pick = [w for w in waiting if w["purpose"] == "PICKUP"]
back = [w for w in waiting if w["purpose"] == "DROPOFF"]

st.subheader(f"{t('Arriving buses')} — {len(pick)} children on board heading to school")
if not pick:
    st.info("No buses with children heading to school right now.")
else:
    for bus_no in sorted({w["bus_no"] for w in pick}):
        rows = [w for w in pick if w["bus_no"] == bus_no]
        with st.container(border=True):
            st.markdown(f"**{bus_no}** · Trip {rows[0]['trip_no']} · {len(rows)} child(ren)")
            sel = st.multiselect("Children handed over", [w["id"] for w in rows], default=[w["id"] for w in rows], key=f"rc_{bus_no}",
                                 format_func=lambda i, rows=rows: next(f"{w['name']} · {w['class']}" for w in rows if w["id"] == i))
            if st.button(f"✅ Confirm received ({len(sel)})", key=f"rcb_{bus_no}", type="primary", disabled=not sel):
                out = []
                for w in [w for w in rows if w["id"] in sel]:
                    ctx = {"operator": receiver, "role": "receiving", "stop_id": SCHOOL_STOP, "method": "STAFF_CONFIRM",
                           "captured_ts": db.ts(), "client_uuid": str(uuid.uuid4()), "received_by": receiver, **gps_fix(SCHOOL_STOP)}
                    res = E.process(w["run_id"], "CHECK_OUT", ctx, student_id=w["student_id"])
                    out.append((res["level"], res["message"]))
                st.session_state.rc_last = out
                st.rerun()

if back:
    st.subheader("Drop-off children still on board")
    st.caption("If a bus brings a child back (no approved recipient at the stop), the crew checks them out at school to you — "
               "the child is then in school custody and the office is alerted.")
    lines_table(back, ("name", "class", "stop_name", "status"))

st.subheader("Received today")
rows = db.q("""SELECT substr(m.exit_ts,12,5) AS Time, b.bus_no AS Bus, r.trip_no AS Trip, s.name AS Student, s.class_name||s.section AS Class,
               m.received_by AS "Received by", CASE m.handover WHEN 'RETURNED_TO_SCHOOL' THEN 'Returned (not released)' ELSE 'Arrival' END AS Type
               FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
               WHERE m.handover IN ('SCHOOL_RECEIPT','RETURNED_TO_SCHOOL') AND r.date=? ORDER BY m.exit_ts DESC""", (db.today(),))
if rows:
    st.dataframe(rows, hide_index=True, width="stretch")
else:
    st.caption("Nothing yet today.")
