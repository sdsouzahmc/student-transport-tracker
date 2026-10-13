"""Setup & data (spec §2, §9): data source / workbook import, students & contacts, badges (issue / revoke / replace) and ID cards,
buses & crew, stops, validated CSV import, devices, users, backups, demo tools."""
import io
import os
import random
import sqlite3
import tempfile
from datetime import date

import pandas as pd
import streamlit as st

import db
import engine as E
from dataset_import import import_workbook
from db import TRIPS
from qr_utils import id_cards_pdf
from ui import msg

user = st.session_state.user
role = user["role"]
st.title("⚙️ Setup & data")
if "su_last" in st.session_state:
    lv, m = st.session_state.pop("su_last")
    msg(lv, m)

tabs = st.tabs(["Data source", "Students", "Badges & ID cards", "Buses & crew", "Stops", "CSV import", "Devices", "Users", "Backup", "Demo tools", "Reset trip data"])
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(APP_DIR, "School_Bus_Trip_Dataset.xlsx")


def new_badge(sid, code):
    return f"BDG-{code[-4:] if code[-4:].isdigit() else sid:0>4}-{random.randint(1000, 9999)}"


# ------------------------------------------------------------------ data source
with tabs[0]:
    st.markdown(f"**Current data:** {db.setting('data_source', '—')}")
    k = db.q1("""SELECT (SELECT COUNT(*) FROM students) s, (SELECT COUNT(*) FROM buses) b, (SELECT COUNT(*) FROM trip_runs) t,
                 (SELECT COUNT(*) FROM events) e, (SELECT MIN(date) FROM trip_runs) d1, (SELECT MAX(date) FROM trip_runs) d2""")
    st.caption(f"{k['s']} students · {k['b']} buses · {k['t']} trips · {k['e']} scan events · trip dates {k['d1'] or '—'} to {k['d2'] or '—'}")
    with st.form("school_name_form", border=False):
        cN1, cN2 = st.columns([3, 1], vertical_alignment="bottom")
        nm = cN1.text_input("School name (shown on dashboard, sidebar and ID cards)", value=db.setting("school_name", db.SCHOOL_NAME),
                            disabled=role != "admin")
        if cN2.form_submit_button("Save name", disabled=role != "admin") and nm.strip():
            db.set_setting("school_name", nm.strip())
            db.audit(user["username"], role, "SETTING", "setting", "school_name", nm.strip())
            st.rerun()
    if role != "admin":
        st.info("Only the administrator can replace the data.")
    else:
        st.warning("Loading data replaces everything in the database (trips, scans, users). Download a backup first if needed.")
        c1, c2 = st.columns(2)
        with c1, st.container(border=True):
            st.markdown("**Supplied dataset** — School_Bus_Trip_Dataset.xlsx  \n120 students · 3 buses · 4–8 Oct 2026 · labelled test cases")
            shift = st.checkbox("If the sample dates are in the future, move them back by whole weeks", value=True, key="ds_shift")
            if st.button("Load supplied dataset", disabled=not os.path.exists(DATASET)):
                res, err, warn = import_workbook(DATASET, "School_Bus_Trip_Dataset.xlsx", shift)
                st.session_state.su_last = ("ok", f"Loaded: {res}") if res else ("error", "; ".join(err))
                st.rerun()
        with c2, st.container(border=True):
            st.markdown("**Full-size generated demo**  \n1,250 students · 28 buses · Doha stops (then use Demo tools to create trips)")
            if st.button("Generate full-size demo"):
                db.init_db(reset=True)
                st.session_state.su_last = ("ok", "Full-size demo generated. Use Demo tools to simulate trips.")
                st.rerun()
        with st.container(border=True):
            st.markdown("**Upload a workbook** in the same layout (sheets Students, Crew, Locations, Trips, StudentTrips, ScanEvents, Exceptions, "
                        "Notifications). It is validated first; nothing is loaded if there are errors.")
            up = st.file_uploader("Workbook (.xlsx)", type=["xlsx"])
            if up and st.button("Validate and load"):
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
                    tf.write(up.getvalue())
                res, err, warn = import_workbook(tf.name, up.name)
                if err:
                    st.error("Not loaded — fix these problems:")
                    st.write(err)
                else:
                    st.session_state.su_last = ("ok", f"Loaded {up.name}: {res}" + (f" · warnings: {len(warn)}" if warn else ""))
                    st.rerun()

# ------------------------------------------------------------------ students
with tabs[1]:
    q_ = st.text_input("Search student (name or ID)")
    rows = db.q("""SELECT s.id, s.code AS ID, s.name AS Name, s.class_name||s.section AS Class, s.batch AS Batch, s.grp AS "Group", b.bus_no AS Bus,
                   st.name AS Stop, CASE s.active WHEN 1 THEN 'Active' ELSE 'Inactive' END AS Status,
                   (SELECT COUNT(*) FROM recipients r WHERE r.student_id=s.id AND r.active=1) AS Recipients
                   FROM students s LEFT JOIN buses b ON b.id=s.bus_id LEFT JOIN stops st ON st.id=s.stop_id
                   WHERE s.name LIKE ? OR s.code LIKE ? ORDER BY s.code LIMIT 300""", (f"%{q_}%", f"%{q_}%"))
    st.dataframe(pd.DataFrame(rows).drop(columns=["id"]) if rows else pd.DataFrame(), hide_index=True, width="stretch", height=260)
    if rows:
        sid = st.selectbox("Edit student", [r["id"] for r in rows], format_func=lambda i, rows=rows: next(f"{r['ID']} — {r['Name']}" for r in rows if r["id"] == i))
        s = db.q1("SELECT * FROM students WHERE id=?", (sid,))
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns(4)
            bl = db.buses()
            nb = c1.selectbox("Bus", [b["id"] for b in bl], index=[b["id"] for b in bl].index(s["bus_id"]) if s["bus_id"] in [b["id"] for b in bl] else 0,
                              format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i), key=f"eb{sid}")
            sl = db.stops_of_bus(nb)
            ns = c2.selectbox("Stop", [x["id"] for x in sl], index=[x["id"] for x in sl].index(s["stop_id"]) if s["stop_id"] in [x["id"] for x in sl] else 0,
                              format_func=lambda i: next(x["name"] for x in sl if x["id"] == i), key=f"es{sid}") if sl else None
            nbat = c3.selectbox("Batch", [1, 2], index=s["batch"] - 1, key=f"ebt{sid}")
            ngrp = c4.selectbox("Group", ["Junior", "Senior"], index=0 if s["grp"] == "Junior" else 1, key=f"eg{sid}")
            act = st.checkbox("Active (uses transport)", value=bool(s["active"]), key=f"ea{sid}")
            if st.button("Save student", key=f"esv{sid}"):
                db.x("UPDATE students SET bus_id=?, stop_id=?, batch=?, grp=?, active=? WHERE id=?", (nb, ns, nbat, ngrp, int(act), sid))
                db.audit(user["username"], role, "STUDENT_UPDATED", "student", s["code"], f"bus {nb} stop {ns} batch {nbat} {ngrp} active {act}")
                st.session_state.su_last = ("ok", f"{s['name']} updated.")
                st.rerun()
            st.markdown("**Guardians**")
            st.dataframe(db.q("""SELECT g.name AS Name, g.relation AS Relation, g.phone AS Phone, g.email AS "E-mail",
                                 CASE g.verified WHEN 1 THEN 'Verified' ELSE 'Not verified' END AS Status, u.username AS Login
                                 FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id LEFT JOIN users u ON u.guardian_id=g.id
                                 WHERE sg.student_id=?""", (sid,)), hide_index=True, width="stretch")
            st.markdown("**Authorized recipients** (only these people can receive the child at the stop)")
            recs = db.q("SELECT id, name AS Name, relation AS Relation, phone AS Phone, id_number AS \"ID no.\", active FROM recipients WHERE student_id=?", (sid,))
            st.dataframe([{k: v for k, v in r.items() if k not in ("id",)} for r in recs], hide_index=True, width="stretch")
            r1, r2 = st.columns(2)
            with r1.popover("➕ Add recipient"):
                rn = st.text_input("Name", key=f"rn{sid}")
                rr = st.selectbox("Relation", ["Father", "Mother", "Guardian", "Nanny", "Driver", "Relative"], key=f"rr{sid}")
                rp = st.text_input("Phone", key=f"rp{sid}")
                ri = st.text_input("QID / ID number", key=f"ri{sid}")
                if st.button("Add", key=f"ra{sid}", disabled=not rn.strip()):
                    db.x("INSERT INTO recipients(student_id, name, relation, phone, id_number) VALUES (?,?,?,?,?)", (sid, rn, rr, rp, ri))
                    db.audit(user["username"], role, "RECIPIENT_ADDED", "student", s["code"], f"{rn} ({rr})")
                    st.rerun()
            act_recs = [r for r in recs if r["active"]]
            if act_recs:
                with r2.popover("➖ Remove recipient"):
                    rid = st.selectbox("Recipient", [r["id"] for r in act_recs], format_func=lambda i: next(r["Name"] for r in act_recs if r["id"] == i), key=f"rx{sid}")
                    if st.button("Remove", key=f"rxb{sid}"):
                        db.x("UPDATE recipients SET active=0 WHERE id=?", (rid,))
                        db.audit(user["username"], role, "RECIPIENT_REMOVED", "student", s["code"], str(rid))
                        st.rerun()
            with st.popover("👨‍👩‍👧 Link an existing guardian (siblings)"):
                gq = st.text_input("Guardian name", key=f"lg{sid}", placeholder="e.g. Guardian 051")
                gl = db.q("SELECT id, name, email FROM guardians WHERE name LIKE ? ORDER BY name LIMIT 20", (f"%{gq}%",)) if gq else []
                if gl:
                    gpick = st.selectbox("Guardian", [g["id"] for g in gl], format_func=lambda i: next(f"{g['name']} · {g['email']}" for g in gl if g["id"] == i), key=f"lgs{sid}")
                    if st.button("Link as guardian + authorized recipient", key=f"lgb{sid}"):
                        E.link_guardian(sid, gpick, user["username"], role)
                        st.session_state.su_last = ("ok", "Guardian linked — they can now receive both children and see both in the parent portal.")
                        st.rerun()
            st.markdown("**Emergency contacts**")
            st.dataframe(db.q("SELECT name AS Name, relation AS Relation, phone AS Phone FROM emergency_contacts WHERE student_id=?", (sid,)),
                         hide_index=True, width="stretch")

# ------------------------------------------------------------------ badges
with tabs[2]:
    q2 = st.text_input("Find student", key="bq")
    rows = db.q("""SELECT s.id, s.code, s.name, b.code AS badge, b.status FROM students s LEFT JOIN badges b ON b.student_id=s.id AND b.status='ACTIVE'
                   WHERE s.name LIKE ? OR s.code LIKE ? ORDER BY s.code LIMIT 200""", (f"%{q2}%", f"%{q2}%"))
    if rows:
        sid = st.selectbox("Student", [r["id"] for r in rows], format_func=lambda i, rows=rows: next(f"{r['code']} — {r['name']} · badge {r['badge'] or 'NONE'}" for r in rows if r["id"] == i))
        r = next(r for r in rows if r["id"] == sid)
        hist = db.q("SELECT code AS Badge, status AS Status, issued_at AS Issued, revoked_at AS Revoked, reason AS Reason, replaced_by AS \"Replaced by\" "
                    "FROM badges WHERE student_id=? ORDER BY id DESC", (sid,))
        st.dataframe(hist, hide_index=True, width="stretch")
        c1, c2, c3 = st.columns(3)
        why = c1.selectbox("Reason", ["Lost card", "Damaged", "Stolen", "Left school", "Security"], key="bwhy")
        if c2.button("🔁 Replace badge (revoke + issue new)", disabled=not r["badge"]):
            nc = new_badge(sid, r["code"])
            db.x("UPDATE badges SET status='REVOKED', revoked_at=?, reason=?, replaced_by=? WHERE code=?", (db.ts(), why, nc, r["badge"]))
            db.x("INSERT INTO badges(code, student_id, status, issued_at) VALUES (?,?,?,?)", (nc, sid, "ACTIVE", db.ts()))
            db.audit(user["username"], role, "BADGE_REPLACED", "badge", r["badge"], f"-> {nc} ({why})")
            st.session_state.su_last = ("ok", f"Badge {r['badge']} revoked; new badge {nc} issued. The old one is now rejected at scan.")
            st.rerun()
        if r["badge"] and c3.button("⛔ Revoke only"):
            db.x("UPDATE badges SET status='REVOKED', revoked_at=?, reason=? WHERE code=?", (db.ts(), why, r["badge"]))
            db.audit(user["username"], role, "BADGE_REVOKED", "badge", r["badge"], why)
            st.rerun()
        if not r["badge"] and c3.button("➕ Issue badge"):
            nc = new_badge(sid, r["code"])
            db.x("INSERT INTO badges(code, student_id, status, issued_at) VALUES (?,?,?,?)", (nc, sid, "ACTIVE", db.ts()))
            db.audit(user["username"], role, "BADGE_ISSUED", "badge", nc, r["code"])
            st.rerun()
    st.divider()
    st.markdown("**Printable QR ID cards** (10 per A4 page)")
    bl = db.buses()
    bsel = st.selectbox("Bus", [b["id"] for b in bl], format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i), key="idbus")
    cards = db.q("""SELECT s.code, s.name, s.class_name, s.section, s.batch, s.grp, bu.bus_no, st.name AS stop_name, b.code AS badge FROM students s
                    JOIN badges b ON b.student_id=s.id AND b.status='ACTIVE' JOIN buses bu ON bu.id=s.bus_id LEFT JOIN stops st ON st.id=s.stop_id
                    WHERE s.bus_id=? AND s.active=1 ORDER BY st.seq, s.name""", (bsel,))
    if cards:
        st.download_button(f"⬇ ID cards PDF ({len(cards)} cards)", id_cards_pdf(cards, db.setting("school_name", db.SCHOOL_NAME)),
                           file_name=f"ID_cards_{cards[0]['bus_no']}.pdf", mime="application/pdf")

# ------------------------------------------------------------------ buses & crew
with tabs[3]:
    rows = db.q("""SELECT b.id, b.bus_no AS Bus, b.plate AS Plate, b.capacity AS Capacity, b.route_name AS Route, b.fvts_vehicle AS "Autotrace vehicle",
                   d.name AS Driver, s.name AS Supervisor, c.name AS "Care-taker",
                   (SELECT COUNT(*) FROM students x WHERE x.bus_id=b.id AND x.active=1) AS Students
                   FROM buses b LEFT JOIN staff d ON d.id=b.driver_id LEFT JOIN staff s ON s.id=b.supervisor_id LEFT JOIN staff c ON c.id=b.caretaker_id
                   ORDER BY b.bus_no""")
    st.dataframe(pd.DataFrame(rows).drop(columns=["id"]), hide_index=True, width="stretch")
    bid = st.selectbox("Edit bus", [r["id"] for r in rows], format_func=lambda i, rows=rows: next(r["Bus"] for r in rows if r["id"] == i))
    b = db.bus(bid)
    c1, c2, c3, c4 = st.columns(4)
    cap = c1.number_input("Capacity", 10, 80, b["capacity"])

    def pick(col, label, r, cur):
        opts = db.q("SELECT id, name FROM staff WHERE role=? ORDER BY name", (r,))
        ids = [o["id"] for o in opts]
        return col.selectbox(label, ids, index=ids.index(cur) if cur in ids else 0, format_func=lambda i: next(o["name"] for o in opts if o["id"] == i), key=f"bc{r}{bid}")
    nd = pick(c2, "Driver", "driver", b["driver_id"])
    ns = pick(c3, "Supervisor", "supervisor", b["supervisor_id"])
    nc = pick(c4, "Care-taker", "caretaker", b["caretaker_id"])
    fv = st.text_input("Autotrace (FVTS) vehicle name — used to pull live GPS in production", value=b["fvts_vehicle"] or "")
    if st.button("Save bus"):
        db.x("UPDATE buses SET capacity=?, driver_id=?, supervisor_id=?, caretaker_id=?, fvts_vehicle=? WHERE id=?", (cap, nd, ns, nc, fv, bid))
        db.audit(user["username"], role, "BUS_UPDATED", "bus", b["bus_no"], f"cap {cap} crew {nd}/{ns}/{nc}")
        st.session_state.su_last = ("ok", f"{b['bus_no']} saved.")
        st.rerun()

# ------------------------------------------------------------------ stops
with tabs[4]:
    st.dataframe(db.q("""SELECT b.bus_no AS Bus, s.seq AS Seq, s.name AS Stop, s.area AS Area, ROUND(s.lat,5) AS Lat, ROUND(s.lon,5) AS Lon,
                         (SELECT COUNT(*) FROM students x WHERE x.stop_id=s.id AND x.active=1) AS Students
                         FROM stops s LEFT JOIN buses b ON b.id=s.bus_id ORDER BY b.bus_no, s.seq"""), hide_index=True, width="stretch", height=420)
    st.caption(f"School location: {db.school_pos()}. Schedules: " + " · ".join(f"Trip {n} {v['window'][0]}–{v['window'][1]}" for n, v in TRIPS.items()))

# ------------------------------------------------------------------ CSV import
with tabs[5]:
    st.markdown("**Student import (CSV)** — adds new students or updates existing ones (matched by ID). Every row is validated first; "
                "nothing is saved if any row has an error.")
    cols = ["code", "name", "class_name", "section", "batch", "grp", "bus_no", "stop_name", "guardian_name", "guardian_email", "guardian_phone",
            "recipient_name", "recipient_relation", "recipient_phone"]
    ex_bus = db.q1("SELECT b.bus_no, s.name FROM buses b JOIN stops s ON s.bus_id=b.id LIMIT 1") or {"bus_no": "BUS-01", "name": "Stop 1"}
    tmpl = pd.DataFrame([["STU9001", "New Student", "Grade 2", "A", 1, "Junior", ex_bus["bus_no"], ex_bus["name"], "Parent Name", "parent@example.com",
                          "+974 5555 0000", "Parent Name", "Father", "+974 5555 0000"]], columns=cols)
    st.download_button("⬇ Template CSV", tmpl.to_csv(index=False).encode(), file_name="students_template.csv", mime="text/csv")
    up = st.file_uploader("Students CSV", type=["csv"], key="csvup")
    if up:
        try:
            df = pd.read_csv(up, dtype=str).fillna("")
        except Exception as e:      # noqa: BLE001
            st.error(f"Cannot read CSV: {e}")
            df = None
        if df is not None:
            errs = []
            miss = [c for c in cols[:8] if c not in df.columns]
            if miss:
                errs.append(f"Missing columns: {', '.join(miss)}")
            else:
                buses = {b["bus_no"]: b["id"] for b in db.buses()}
                stops = {(s["bus_id"], s["name"]): s["id"] for s in db.q("SELECT id, bus_id, name FROM stops")}
                dupe = df["code"][df["code"].duplicated()].tolist()
                if dupe:
                    errs.append(f"Duplicate IDs in file: {dupe[:5]}")
                for i, r in df.iterrows():
                    n = i + 2
                    if not r["code"].strip() or not r["name"].strip():
                        errs.append(f"Row {n}: code and name are required")
                    if r["batch"] not in ("1", "2"):
                        errs.append(f"Row {n}: batch must be 1 or 2")
                    if r["grp"] not in ("Junior", "Senior"):
                        errs.append(f"Row {n}: grp must be Junior or Senior")
                    if r["bus_no"] not in buses:
                        errs.append(f"Row {n}: unknown bus {r['bus_no']}")
                    elif (buses[r["bus_no"]], r["stop_name"]) not in stops:
                        errs.append(f"Row {n}: stop '{r['stop_name']}' is not on {r['bus_no']}")
                    if "guardian_email" in df and r.get("guardian_email") and "@" not in r["guardian_email"]:
                        errs.append(f"Row {n}: invalid e-mail")
            st.dataframe(df.head(50), hide_index=True, width="stretch")
            if errs:
                st.error(f"{len(errs)} problem(s) — fix the file and upload again:")
                st.write(errs[:40])
            else:
                existing = {s["code"] for s in db.q("SELECT code FROM students")}
                n_new = sum(1 for c in df["code"] if c not in existing)
                st.success(f"Valid: {n_new} new, {len(df) - n_new} updates.")
                if st.button("Import", type="primary"):
                    with db.tx() as c:
                        for _, r in df.iterrows():
                            bid, sid_ = buses[r["bus_no"]], stops[(buses[r["bus_no"]], r["stop_name"])]
                            if r["code"] in existing:
                                c.execute("UPDATE students SET name=?, class_name=?, section=?, batch=?, grp=?, bus_id=?, stop_id=? WHERE code=?",
                                          (r["name"], r["class_name"], r["section"], int(r["batch"]), r["grp"], bid, sid_, r["code"]))
                                continue
                            new_id = c.execute("INSERT INTO students(code, name, class_name, section, batch, grp, bus_id, stop_id) VALUES (?,?,?,?,?,?,?,?)",
                                               (r["code"], r["name"], r["class_name"], r["section"], int(r["batch"]), r["grp"], bid, sid_)).lastrowid
                            if r.get("guardian_name"):
                                gid = c.execute("INSERT INTO guardians(name, phone, email, relation, verified) VALUES (?,?,?,?,0)",
                                                (r["guardian_name"], r.get("guardian_phone", ""), r.get("guardian_email", ""), "Guardian")).lastrowid
                                c.execute("INSERT INTO student_guardians VALUES (?,?,1)", (new_id, gid))
                            if r.get("recipient_name"):
                                c.execute("INSERT INTO recipients(student_id, name, relation, phone) VALUES (?,?,?,?)",
                                          (new_id, r["recipient_name"], r.get("recipient_relation", ""), r.get("recipient_phone", "")))
                            c.execute("INSERT INTO badges(code, student_id, status, issued_at) VALUES (?,?,?,?)",
                                      (new_badge(new_id, r["code"]), new_id, "ACTIVE", db.ts()))
                        db.audit(user["username"], role, "CSV_IMPORT", "students", up.name, f"{n_new} new, {len(df) - n_new} updated", c)
                    st.session_state.su_last = ("ok", f"Imported {n_new} new and updated {len(df) - n_new} students. New guardians start as "
                                                      "'not verified' until the school verifies them.")
                    st.rerun()

# ------------------------------------------------------------------ devices
with tabs[6]:
    devs = db.q("SELECT d.*, b.bus_no FROM devices d LEFT JOIN buses b ON b.id=d.bus_id ORDER BY b.bus_no")
    st.dataframe([{"Device": d["name"], "Bus": d["bus_no"], "Status": d["status"], "Registered": d["registered_at"], "Revoked": d["revoked_at"],
                   "Reason": d["reason"]} for d in devs], hide_index=True, width="stretch")
    did = st.selectbox("Device", [d["id"] for d in devs], format_func=lambda i: next(f"{d['name']} ({d['status']})" for d in devs if d["id"] == i))
    d = next(d for d in devs if d["id"] == did)
    if d["status"] == "ACTIVE":
        why = st.text_input("Reason for revoking", placeholder="Lost / stolen / replaced")
        if st.button("⛔ Revoke device", disabled=not why.strip()):
            db.x("UPDATE devices SET status='REVOKED', revoked_at=?, reason=? WHERE id=?", (db.ts(), why, did))
            db.audit(user["username"], role, "DEVICE_REVOKED", "device", did, why)
            st.session_state.su_last = ("warn", f"{d['name']} revoked — it can no longer start trips or submit scans.")
            st.rerun()
    elif st.button("Re-activate device"):
        db.x("UPDATE devices SET status='ACTIVE', revoked_at=NULL, reason=NULL WHERE id=?", (did,))
        db.audit(user["username"], role, "DEVICE_REACTIVATED", "device", did)
        st.rerun()

# ------------------------------------------------------------------ users
with tabs[7]:
    st.caption("Roles: administrator, transport manager, dispatcher, driver, supervisor, care-taker, receiving staff, verified guardian. "
               "Administrator and transport manager sign in with a second factor (MFA). PINs are stored hashed.")
    us = db.q("SELECT username, role, display_name, active FROM users ORDER BY role, username")
    st.dataframe([{"Login": u["username"], "Role": u["role"], "Name": u["display_name"], "MFA": "Yes" if u["role"] in db.MFA_ROLES else "",
                   "Active": "Yes" if u["active"] else "No"} for u in us], hide_index=True, width="stretch", height=300)
    if role == "admin":
        un = st.selectbox("User", [u["username"] for u in us])
        c1, c2 = st.columns(2)
        newpin = c1.text_input("New PIN / password", type="password")
        if c1.button("Reset PIN", disabled=len(newpin) < 4):
            db.x("UPDATE users SET pin_hash=? WHERE username=?", (db.pin_hash(newpin), un))
            db.audit(user["username"], role, "PIN_RESET", "user", un)
            st.session_state.su_last = ("ok", f"PIN reset for {un}.")
            st.rerun()
        cur = next(u for u in us if u["username"] == un)
        if c2.button("Deactivate" if cur["active"] else "Activate", disabled=un == user["username"]):
            db.x("UPDATE users SET active=? WHERE username=?", (0 if cur["active"] else 1, un))
            db.audit(user["username"], role, "USER_" + ("DEACTIVATED" if cur["active"] else "ACTIVATED"), "user", un)
            st.rerun()
        unv = db.q("SELECT id, name, email FROM guardians WHERE verified=0")
        if unv:
            st.markdown(f"**Guardians awaiting verification ({len(unv)})**")
            gv = st.selectbox("Guardian", [g["id"] for g in unv], format_func=lambda i: next(f"{g['name']} · {g['email']}" for g in unv if g["id"] == i))
            if st.button("Mark verified (identity checked by school)"):
                db.x("UPDATE guardians SET verified=1 WHERE id=?", (gv,))
                db.audit(user["username"], role, "GUARDIAN_VERIFIED", "guardian", gv)
                st.rerun()

# ------------------------------------------------------------------ backup
with tabs[8]:
    st.caption("Full database backup (spec §9). In production: automated encrypted nightly backups with off-site copies and restore tests.")
    if st.button("Prepare backup"):
        src = sqlite3.connect(db.DB_PATH)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        dst = sqlite3.connect(path)
        src.backup(dst)
        dst.close()
        src.close()
        st.session_state.backup = open(path, "rb").read()
        db.audit(user["username"], role, "BACKUP", "database", "", f"{len(st.session_state.backup)} bytes")
    if st.session_state.get("backup"):
        st.download_button("⬇ Download backup", st.session_state.backup, file_name=f"school_bus_backup_{db.today()}.db", mime="application/octet-stream")

# ------------------------------------------------------------------ demo tools
with tabs[9]:
    st.caption("Generate realistic activity through the real rules engine (same checks as the crew app).")
    bl = db.buses()
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**Completed trips for a date**")
        d = st.date_input("Date", value=date.fromisoformat(db.today()), key="dm_d").isoformat()
        trips = st.multiselect("Trips", list(TRIPS), default=list(TRIPS), key="dm_t")
        if st.button("Simulate"):
            with st.spinner("Simulating…"):
                n = E.simulate_day(d, trips=trips)
            st.session_state.su_last = ("ok", f"{n} trip(s) simulated for {d}.")
            st.rerun()
    with c2, st.container(border=True):
        st.markdown("**Trips in progress now** (for the live map)")
        tn = st.selectbox("Trip", list(TRIPS), index=E.trip_for_time() - 1, key="dm_lt")
        nb = st.slider("Number of buses", 1, len(bl), min(len(bl), 8))
        if st.button("Start live trips"):
            n = E.simulate_live(tn, [b["id"] for b in bl[:nb]])
            st.session_state.su_last = ("ok", f"{n} bus(es) now on Trip {tn} with children on board (buses that already have an open trip or ran this trip today are skipped).")
            st.rerun()
    with st.container(border=True):
        st.markdown("**Prepare the 10 demo scenarios** — clears today's open trips (closed trips are kept), restores normal crews, adds relief crew "
                    "(Relief Driver / Supervisor / Care-taker) and links siblings Student 051 + Student 054 to Guardian 051")
        if st.button("Prepare demo scenarios", type="primary", disabled=role != "admin"):
            st.session_state.su_last = ("ok", " ".join(E.prepare_demo(user["username"], role)))
            st.rerun()
    with st.container(border=True):
        st.markdown("**History** — last N school days (Sunday–Thursday)")
        nd = st.slider("Days", 1, 10, 3)
        if st.button("Simulate history"):
            with st.spinner("Simulating — about 2–6 s per day…"):
                tot = sum(E.simulate_day(day) for day in E.school_days_back(nd))
            st.session_state.su_last = ("ok", f"{tot} trip(s) simulated over {nd} day(s).")
            st.rerun()


# ------------------------------------------------------------------ reset trip data
with tabs[10]:
    st.markdown("**Reset trip data** — permanently remove trips for a date so they can be run again. "
                "Removes each trip's children list, scans, parent notices and incidents. The reset is recorded in the audit log.  \n"
                "🔒 **Closed trips are final and are never reset.** Only trips that are planned, in progress or pending sync can be removed.")
    if "rs_result" in st.session_state:
        _lv, _m = st.session_state.pop("rs_result")
        msg(_lv, _m)
    if role != "admin":
        st.info("Only the administrator can reset trip data.")
    else:
        bl_ = db.buses()
        r1, r2, r3 = st.columns([1, 1, 1.4])
        rdate = r1.date_input("Date *", value=None, key="rs_date", format="YYYY/MM/DD",
                              help="Required. Pick the date whose trips you want to reset.")
        bopt = [None] + [b["id"] for b in bl_]
        rbus = r2.selectbox("Bus", bopt, key="rs_bus",
                            format_func=lambda i: "All buses" if i is None else next(b["bus_no"] for b in bl_ if b["id"] == i))
        topt = [None] + list(TRIPS)
        rtrip = r3.selectbox("Trip", topt, key="rs_trip",
                             format_func=lambda n: "All trips" if n is None else f"Trip {n} · {TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]} · {TRIPS[n]['label']}")
        if rdate is None:
            st.warning("Select a date to continue — the date is mandatory.")
        else:
            D_ = rdate.isoformat()
            all_ = E.reset_scope(D_, rbus, rtrip)
            force = st.toggle("Testing only: also reset closed trips (overrides the lock — recorded in the audit log)", key="rs_force")
            runs_ = [r for r in all_ if force or r["status"] != "CLOSED"]
            locked_ = [] if force else [r for r in all_ if r["status"] == "CLOSED"]
            pc_ = E.parent_changes_scope(D_, rbus, rtrip)
            n_pc = len(pc_["absences"]) + len(pc_["change_requests"])
            if locked_:
                st.markdown(f"**🔒 {len(locked_)} closed trip(s) — locked, will be kept:**")
                st.dataframe(pd.DataFrame([{"Bus": r["bus_no"], "Trip": r["trip_no"], "Status": "Closed (locked)",
                                            "Started": (r["start_ts"] or "")[11:16], "Ended": (r["end_ts"] or "")[11:16],
                                            "Children": r["children"], "Scans": r["scans"]} for r in locked_]),
                             hide_index=True, width="stretch")
            if not runs_ and locked_:
                st.info("Every matching trip is closed, so there is nothing that can be reset.")
            elif not runs_:
                st.info(f"No trips found for {D_}" + (f" · {next(b['bus_no'] for b in bl_ if b['id'] == rbus)}" if rbus else "")
                        + (f" · Trip {rtrip}" if rtrip else "") + ". Nothing to reset.")
            else:
                st.markdown(f"**{len(runs_)} trip(s) will be removed:**")
                st.dataframe(pd.DataFrame([{"Bus": r["bus_no"], "Trip": r["trip_no"], "Status": r["status"].replace("_", " ").title(),
                                            "Started": (r["start_ts"] or "")[11:16], "Ended": (r["end_ts"] or "")[11:16],
                                            "Children": r["children"], "On board now": r["onboard"], "Scans": r["scans"],
                                            "Notices": r["notices"], "Incidents": r["incidents"]} for r in runs_]),
                             hide_index=True, width="stretch")
                if any(r["onboard"] for r in runs_):
                    st.warning("Some of these trips still show children on board. Reset only if this is test data.")
            also = False
            if n_pc:   # only offered when there is something to remove
                also = st.checkbox(f"Optional: also remove parent absences and change requests for this date ({n_pc} found)", value=False,
                                   key="rs_pc")
            can = bool(runs_) or bool(also and n_pc)
            if can:
                st.markdown("**Step 2 — confirm:** tick the box below, then press **Reset trip data**.")
            sure = st.checkbox("I understand this permanently deletes the selected trip data", key="rs_ok", disabled=not can)
            if st.button("Reset trip data", type="primary", disabled=not sure or (not runs_ and not (also and n_pc)), key="rs_go"):
                n, m, k = E.reset_trips(D_, user["username"], role, bus_id=rbus, trip_no=rtrip, parent_changes=also, include_closed=force)
                st.session_state.rs_result = ("ok", f"Reset done for {D_}: {n} trip(s) removed"
                                            + (f", {m} absence/change request(s) removed" if also else "") + (f", {k} closed trip(s) kept" if k else "")
                                            + ". The removed trip(s) can now be started again.")
                for k_ in ("rs_ok", "rs_pc", "rs_force"):
                    st.session_state.pop(k_, None)
                st.rerun()
