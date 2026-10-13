"""Office: incidents with assignment / escalation / contact log / resolution; transfer approvals; change requests; corrections;
allocation conflicts."""
from datetime import date

import pandas as pd
import streamlit as st

import db
import engine as E
from ui import msg

user = st.session_state.user
role = user["role"]
st.title("Incidents & approvals")
if "ex_last" in st.session_state:
    msg(*st.session_state.pop("ex_last"))

n_inc = db.q1("SELECT COUNT(*) n FROM incidents WHERE status<>'RESOLVED'")["n"]
n_tr = db.q1("SELECT COUNT(*) n FROM manifest WHERE status='TRANSFER_PENDING'")["n"]
n_ch = db.q1("SELECT COUNT(*) n FROM change_requests WHERE status='PENDING'")["n"]
n_co = db.q1("SELECT COUNT(*) n FROM corrections WHERE status='PENDING'")["n"]
tabs = st.tabs([f"Incidents ({n_inc} open)", f"Transfers ({n_tr})", f"Change requests ({n_ch})", f"Corrections ({n_co})", "Allocation check"])

SEV = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
with tabs[0]:
    show_all = st.toggle("Show resolved too")
    inc = db.q(f"""SELECT i.*, b.bus_no, s.name AS sname, s.code, r.trip_no, r.date FROM incidents i LEFT JOIN buses b ON b.id=i.bus_id
                   LEFT JOIN students s ON s.id=i.student_id LEFT JOIN trip_runs r ON r.id=i.run_id
                   {'' if show_all else "WHERE i.status<>'RESOLVED'"}
                   ORDER BY CASE i.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, i.id DESC LIMIT 500""")
    if not inc:
        st.success("No open incidents.")
    else:
        st.dataframe(pd.DataFrame([{"ID": i["id"], "Time": i["ts"][:16], "Severity": i["severity"], "Type": i["kind"].replace("_", " "),
                                    "Bus": i["bus_no"], "Trip": i["trip_no"], "Student": i["sname"], "Status": i["status"],
                                    "Assigned to": i["assigned_to"], "Details": i["details"]} for i in inc]), hide_index=True, width="stretch", height=260)
        iid = st.selectbox("Open incident", [i["id"] for i in inc], format_func=lambda x: next(
            f"#{i['id']} {i['severity']} {i['kind'].replace('_', ' ')} — {i['sname'] or i['bus_no'] or ''}" for i in inc if i["id"] == x))
        i = next(i for i in inc if i["id"] == iid)
        with st.container(border=True):
            st.markdown(f"**#{i['id']} {i['kind'].replace('_', ' ')}** · {i['severity']} · {i['status']} · {i['ts']}")
            st.write(i["details"])
            if i["resolution"]:
                st.caption(f"Resolution: {i['resolution']} ({i['resolved_ts'] or ''})")
            if i["student_id"]:
                st.markdown("**Contacts for this child**")
                cts = db.q("""SELECT name, relation, phone, 'Guardian' AS type FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id
                              WHERE sg.student_id=? UNION ALL SELECT name, relation, phone, 'Authorized recipient' FROM recipients WHERE student_id=? AND active=1
                              UNION ALL SELECT name, relation, phone, 'Emergency contact' FROM emergency_contacts WHERE student_id=?""",
                           (i["student_id"],) * 3)
                st.dataframe(cts, hide_index=True, width="stretch")
            log = db.q("SELECT ts AS Time, contact_name AS Contact, phone AS Phone, outcome AS Outcome, by_user AS \"By\" FROM incident_contacts "
                       "WHERE incident_id=? ORDER BY id", (iid,))
            if log:
                st.markdown("**Contact log**")
                st.dataframe(log, hide_index=True, width="stretch")
            c1, c2, c3 = st.columns(3)
            with c1.popover("👤 Assign / acknowledge", width="stretch"):
                staff = [s["name"] for s in db.q("SELECT name FROM staff WHERE role IN ('dispatcher','manager','admin','supervisor') ORDER BY role, name")]
                who = st.selectbox("Assign to", staff, key="as_who")
                if st.button("Assign", key="as_go"):
                    db.x("UPDATE incidents SET assigned_to=?, status=CASE WHEN status='OPEN' THEN 'ASSIGNED' ELSE status END, ack_ts=COALESCE(ack_ts,?) WHERE id=?",
                         (who, db.ts(), iid))
                    db.audit(user["username"], role, "INCIDENT_ASSIGNED", "incident", iid, who)
                    st.session_state.ex_last = ("ok", f"Incident #{iid} assigned to {who}.")
                    st.rerun()
            with c2.popover("⬆ Escalate", width="stretch"):
                new = SEV[min(SEV.index(i["severity"]) + 1, 3)] if i["severity"] in SEV else "HIGH"
                st.caption(f"Raise severity {i['severity']} → {new} and notify the transport manager.")
                if st.button("Escalate", key="esc_go"):
                    db.x("UPDATE incidents SET severity=?, status='ESCALATED' WHERE id=?", (new, iid))
                    db.audit(user["username"], role, "INCIDENT_ESCALATED", "incident", iid, f"{i['severity']} -> {new}")
                    st.session_state.ex_last = ("warn", f"Incident #{iid} escalated to {new}.")
                    st.rerun()
            with c3.popover("📞 Log contact", width="stretch"):
                cn = st.text_input("Contact name", key="ct_n")
                ph = st.text_input("Phone", key="ct_p")
                oc = st.selectbox("Outcome", ["Reached — informed", "Reached — will collect", "No answer", "Wrong number", "Voicemail left"], key="ct_o")
                if st.button("Save contact", key="ct_go", disabled=not cn.strip()):
                    db.x("INSERT INTO incident_contacts(incident_id, ts, contact_name, phone, outcome, by_user) VALUES (?,?,?,?,?,?)",
                         (iid, db.ts(), cn, ph, oc, user["display_name"]))
                    db.audit(user["username"], role, "INCIDENT_CONTACT", "incident", iid, f"{cn}: {oc}")
                    st.rerun()
            if i["status"] != "RESOLVED":
                res = st.text_input("Resolution", key="rs_t", placeholder="What was done / where the child is now")
                if st.button("✅ Resolve", disabled=not res.strip()):
                    db.x("UPDATE incidents SET status='RESOLVED', resolution=?, resolved_ts=? WHERE id=?", (res, db.ts(), iid))
                    db.audit(user["username"], role, "INCIDENT_RESOLVED", "incident", iid, res)
                    st.session_state.ex_last = ("ok", f"Incident #{iid} resolved.")
                    st.rerun()

with tabs[1]:
    st.caption("A child who boarded a bus they are not assigned to stays 'awaiting transfer approval'; that trip cannot close until you decide.")
    tr = db.q("""SELECT m.id, m.note, m.entry_ts, s.name, s.code, b.bus_no, r.trip_no, r.date FROM manifest m JOIN students s ON s.id=m.student_id
                 JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id WHERE m.status='TRANSFER_PENDING'""")
    if not tr:
        st.success("No transfers waiting.")
    for x_ in tr:
        with st.container(border=True):
            st.markdown(f"**{x_['name']}** ({x_['code']}) boarded **{x_['bus_no']}** Trip {x_['trip_no']} at {x_['entry_ts'][11:16]} — {x_['note']}")
            c1, c2 = st.columns(2)
            if c1.button("Approve transfer (child rides this bus)", key=f"tra{x_['id']}", type="primary"):
                E.approve_transfer(x_["id"], user["username"], role, True)
                st.session_state.ex_last = ("ok", "Transfer approved — child is now on board this bus; removed from the original bus list.")
                st.rerun()
            if c2.button("Reject (child returned to school)", key=f"trr{x_['id']}"):
                E.approve_transfer(x_["id"], user["username"], role, False)
                st.session_state.ex_last = ("warn", "Transfer rejected — recorded as returned to school.")
                st.rerun()

with tabs[2]:
    ch = db.q("""SELECT c.*, s.name, s.code, st.name AS stop_name, b.bus_no FROM change_requests c JOIN students s ON s.id=c.student_id
                 LEFT JOIN stops st ON st.id=c.new_stop_id LEFT JOIN buses b ON b.id=c.new_bus_id ORDER BY c.status='PENDING' DESC, c.id DESC LIMIT 200""")
    if not ch:
        st.caption("No change requests.")
    for c in [c for c in ch if c["status"] == "PENDING"]:
        what = {"STOP": f"stop → {c['stop_name']}", "BUS": f"bus → {c['bus_no']} ({c['stop_name'] or 'same stop'})",
                "RECIPIENT": f"one-day recipient → {c['recipient_name']} {c['recipient_phone'] or ''}"}.get(c["kind"], c["kind"])
        with st.container(border=True):
            st.markdown(f"**{c['name']}** ({c['code']}) · {c['date']} · {'Trip ' + str(c['trip_no']) if c['trip_no'] else 'all trips'} · {what}  \n"
                        f"<span class='muted'>by {c['requested_by']} · {c['note'] or ''}</span>", unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            if b1.button("Approve", key=f"cha{c['id']}", type="primary"):
                E.decide_change(c["id"], True, user["username"], role)
                st.rerun()
            if b2.button("Reject", key=f"chr{c['id']}"):
                E.decide_change(c["id"], False, user["username"], role)
                st.rerun()
    done = [c for c in ch if c["status"] != "PENDING"]
    if done:
        st.dataframe([{"Date": c["date"], "Student": c["name"], "Type": c["kind"], "Status": c["status"], "By": c["decided_by"]} for c in done],
                     hide_index=True, width="stretch")

with tabs[3]:
    st.caption("Corrections never overwrite the original scan events; the approved value is applied to the trip record and both are kept.")
    with st.expander("➕ Request a correction"):
        d = st.date_input("Trip date", value=date.fromisoformat(db.today()), key="co_d").isoformat()
        runs = db.q("SELECT r.id, r.trip_no, b.bus_no FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.date=? ORDER BY r.trip_no, b.bus_no", (d,))
        if runs:
            rid = st.selectbox("Trip", [r["id"] for r in runs], format_func=lambda i: next(f"{r['bus_no']} Trip {r['trip_no']}" for r in runs if r["id"] == i))
            lines = E.run_lines(rid)
            mid = st.selectbox("Child", [ln["id"] for ln in lines], format_func=lambda i: next(f"{ln['name']} · {ln['status']}" for ln in lines if ln["id"] == i))
            field = st.selectbox("Field", E.CORRECTABLE)
            cur = next(ln for ln in lines if ln["id"] == mid)[field]
            st.caption(f"Current value: {cur}")
            new = st.text_input("Correct value")
            why = st.text_input("Reason")
            if st.button("Submit correction", disabled=not (new.strip() and why.strip())):
                E.request_correction(mid, field, new.strip(), why.strip(), user["username"], role)
                st.session_state.ex_last = ("ok", "Correction submitted for approval.")
                st.rerun()
        else:
            st.caption("No trips on that date.")
    co = db.q("""SELECT c.*, s.name FROM corrections c JOIN manifest m ON m.id=c.manifest_id JOIN students s ON s.id=m.student_id
                 ORDER BY c.status='PENDING' DESC, c.id DESC LIMIT 200""")
    for c in [c for c in co if c["status"] == "PENDING"]:
        with st.container(border=True):
            st.markdown(f"**{c['name']}** · {c['field']}: `{c['old_value']}` → `{c['new_value']}` · {c['reason']} · by {c['requested_by']}")
            same = c["requested_by"] == user["username"]
            b1, b2 = st.columns(2)
            if b1.button("Approve", key=f"coa{c['id']}", type="primary", disabled=same, help="A different person must approve" if same else None):
                E.decide_correction(c["id"], True, user["username"], role)
                st.rerun()
            if b2.button("Reject", key=f"cor{c['id']}"):
                E.decide_correction(c["id"], False, user["username"], role)
                st.rerun()
    if co:
        st.dataframe([{"Requested": c["created_at"], "Student": c["name"], "Field": c["field"], "Original": c["old_value"], "Corrected": c["new_value"],
                       "Status": c["status"], "By": c["requested_by"], "Decided by": c["decided_by"]} for c in co], hide_index=True, width="stretch")

with tabs[4]:
    d = st.date_input("Check date", value=date.fromisoformat(db.today()), key="al_d").isoformat()
    conf = E.allocation_conflicts(d)
    if not conf:
        st.success("No allocation conflicts.")
    else:
        cdf = pd.DataFrame(conf, columns=["Problem", "Item"])
        st.dataframe(cdf.groupby("Problem").size().reset_index(name="Count"), hide_index=True)
        st.dataframe(cdf, hide_index=True, width="stretch", height=300)
