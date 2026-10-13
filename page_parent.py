"""Parent portal (spec §7): linked children only — status, accepted scans, handover, last bus location and update time,
'not coming today', change requests, notices; delayed-offline labels; urgent cases need a direct call."""
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import db
import engine as E
import planner as P
from db import TRIPS
from i18n import t
from ui import bus_map, msg, status_text

user = st.session_state.user
if user["role"] == "guardian":
    gid = user["guardian_id"]
else:   # office users can preview the portal as a guardian
    gl = db.q("""SELECT DISTINCT g.id, g.name, u.username FROM guardians g JOIN users u ON u.guardian_id=g.id ORDER BY u.username LIMIT 200""")
    gid = st.selectbox("Preview as guardian", [g["id"] for g in gl], format_func=lambda i: next(f"{g['username']} — {g['name']}" for g in gl if g["id"] == i))
g = db.q1("SELECT * FROM guardians WHERE id=?", (gid,))
kids = db.q("""SELECT s.*, b.bus_no FROM students s JOIN student_guardians sg ON sg.student_id=s.id LEFT JOIN buses b ON b.id=s.bus_id
               WHERE sg.guardian_id=? AND s.active=1 ORDER BY s.name""", (gid,))
today = db.today()

st.title("👪 " + t("My children"))
st.markdown(f"<div class='warn'>📞 {t('Urgent? Do not rely on the app — call the transport office:')} <b>{db.TRANSPORT_PHONE}</b></div>",
            unsafe_allow_html=True)
if not g["verified"]:
    st.error("Your guardian account is not verified yet. The school must verify you before child details are shown.")
    st.stop()
if not kids:
    st.info("No children are linked to this account.")
    st.stop()
if "pp_last" in st.session_state:
    msg(*st.session_state.pop("pp_last"))


def is_delayed(ev):
    if not ev:
        return False
    if ev.get("connectivity") == "Offline":
        return True
    try:
        return (datetime.strptime(ev["received_ts"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(ev["captured_ts"], "%Y-%m-%d %H:%M:%S")) > timedelta(minutes=1)
    except (TypeError, ValueError):
        return False


for k in kids:
    with st.container(border=True):
        st.markdown(f"### {k['name']}  <span class='pill'>{k['class_name']}{k['section']} · Batch {k['batch']} {k['grp']} · {k['bus_no']}</span>",
                    unsafe_allow_html=True)
        lines = db.q("""SELECT m.*, r.trip_no, r.date, r.status AS run_status, b.bus_no, st.name AS stop_name FROM manifest m
                        JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id LEFT JOIN stops st ON st.id=m.stop_id
                        WHERE m.student_id=? AND r.date=? ORDER BY r.trip_no""", (k["id"], today))
        last_ev = db.q1("""SELECT e.*, b.bus_no, r.trip_no FROM events e JOIN trip_runs r ON r.id=e.run_id JOIN buses b ON b.id=r.bus_id
                           WHERE e.student_id=? AND e.result<>'REJECTED' ORDER BY e.captured_ts DESC LIMIT 1""", (k["id"],))
        c1, c2 = st.columns([5, 2])
        with c1:
            planned = []
            done_trips = {ln["trip_no"] for ln in lines}
            for n in TRIPS:
                p = E.manifest_for_student(k, today, n, None)
                if p and n not in done_trips:
                    planned.append({"Trip": n, t("Bus"): p["bus_no"], "Purpose": p["purpose"].title(),
                                    t("Status"): t("Declared absent") if E.is_absent(k["id"], today, n, None) else f"Planned {TRIPS[n]['window'][0]}",
                                    t("Boarded"): "", t("Got off"): "", t("Handover"): ""})
            rows = [{"Trip": ln["trip_no"], t("Bus"): ln["bus_no"], "Purpose": ln["purpose"].title(), t("Status"): status_text(ln["status"]),
                     t("Boarded"): (ln["entry_ts"] or "")[11:16], t("Got off"): (ln["exit_ts"] or "")[11:16],
                     t("Handover"): (f"{ln['recipient_name']} ({ln['recipient_relation']})" if ln["handover"] == "HOME_RELEASE"
                                     else (f"School — {ln['received_by']}" if ln["received_by"] else ""))} for ln in lines]
            st.markdown("**" + t("Today's journey") + f"** — {today}")
            allrows = sorted(rows + planned, key=lambda r: r["Trip"])
            if allrows:
                st.dataframe(pd.DataFrame(allrows), hide_index=True, width="stretch")
            else:
                st.caption(t("Not travelling today"))
            if last_ev:
                where = "School" if last_ev["stop_id"] == db.SCHOOL_STOP else (db.stop(last_ev["stop_id"]) or {}).get("name", "")
                st.markdown(f"{t('Last update')}: **{last_ev['kind'].replace('_', ' ').title()}** on {last_ev['bus_no']} Trip {last_ev['trip_no']}"
                            f" at {where} — {last_ev['captured_ts'][:16]}")
                if is_delayed(last_ev):
                    st.caption("🕓 " + t("Delayed update — the bus device was offline; this was received later.") + f" (received {last_ev['received_ts'][11:16]})")
        with c2:
            run = db.q1("""SELECT r.* FROM trip_runs r JOIN manifest m ON m.run_id=r.id WHERE m.student_id=? AND r.start_ts IS NOT NULL
                           ORDER BY r.start_ts DESC LIMIT 1""", (k["id"],))
            if run:
                ev = db.q1("""SELECT lat, lon, captured_ts FROM events WHERE run_id=? AND lat IS NOT NULL AND result<>'REJECTED'
                              ORDER BY captured_ts DESC LIMIT 1""", (run["id"],))
                lat, lon, when = (ev["lat"], ev["lon"], ev["captured_ts"]) if ev else (run["start_lat"], run["start_lon"], run["start_ts"])
                if run["end_ts"] and run["end_lat"]:
                    lat, lon, when = run["end_lat"], run["end_lon"], run["end_ts"]
                if lat:
                    st.markdown(f"**{t('Last bus location')}** · {when[:16]}")
                    bus_map([{"bus": k["bus_no"], "lat": lat, "lon": lon, "status": run["status"], "onboard": ""}], height=200, zoom=12)
        # quick actions
        a1, a2 = st.columns(2)
        with a1.popover("🚫 " + "My child is not coming today", width="stretch"):
            st.markdown(f"**{t('Declare absence')} — {k['name']}**")
            d = st.date_input(t("Date"), value=date.fromisoformat(today), min_value=date.fromisoformat(today), key=f"abd{k['id']}")
            trips_for = [n for n in TRIPS if E.manifest_for_student(k, d.isoformat(), n, None)]
            scope = st.radio("Which trips?", [t("All trips")] + [f"Trip {n} only" for n in trips_for], key=f"abs{k['id']}")
            why = st.selectbox(t("Reason"), ["Sick", "Parent will drop / collect", "Family travel", "Appointment", "Other"], key=f"abr{k['id']}")
            if st.button(t("Send"), key=f"abb{k['id']}", type="primary"):
                trip_no = None if scope == t("All trips") else int(scope.split()[1])
                E.declare_absence(k["id"], d.isoformat(), trip_no, why, user["username"], "GUARDIAN")
                _which = [n for n in trips_for if trip_no is None or n == trip_no]
                _det = []
                for n in _which:
                    _m = E.manifest_for_student(k, d.isoformat(), n, None)
                    if _m:
                        _det.append(f"{_m['bus_no']} · Trip {n} ({'pickup' if _m['purpose'] == 'PICKUP' else 'drop-off'}, "
                                    f"{TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]})")
                st.session_state.pp_last = ("ok", f"{k['name']}: {t('Absence recorded. The crew will not wait for your child.')}"
                                            + f"<br><b>{d:%a %d %b %Y}</b> — " + ("; ".join(_det) if _det else t("All trips")))
                st.rerun()
        with a2.popover("✏️ " + t("Request a change"), width="stretch"):
            d = st.date_input(t("Date"), value=date.fromisoformat(today), min_value=date.fromisoformat(today), key=f"chd{k['id']}")
            kind = st.radio(t("Change type"), ["STOP", "BUS", "RECIPIENT"], key=f"chk{k['id']}",
                            format_func={"STOP": t("Different stop"), "BUS": t("Different bus"), "RECIPIENT": t("One-day recipient")}.get)
            trip = st.selectbox(t("Trip"), [None] + list(TRIPS), format_func=lambda n: t("All trips") if n is None else f"Trip {n}", key=f"cht{k['id']}")
            new_stop = new_bus = rname = rphone = None
            if kind == "STOP":
                sl = db.stops_of_bus(k["bus_id"])
                new_stop = st.selectbox("New stop", [s["id"] for s in sl], format_func=lambda i: next(s["name"] for s in sl if s["id"] == i), key=f"chs{k['id']}")
            elif kind == "BUS":
                bl = db.buses()
                new_bus = st.selectbox(t("Bus"), [b["id"] for b in bl if b["id"] != k["bus_id"]],
                                       format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i), key=f"chb{k['id']}")
                sl = db.stops_of_bus(new_bus)
                new_stop = st.selectbox("Stop", [s["id"] for s in sl], format_func=lambda i: next(s["name"] for s in sl if s["id"] == i), key=f"chbs{k['id']}") if sl else None
            else:
                rname = st.text_input(t("Recipient name"), key=f"chn{k['id']}")
                rphone = st.text_input(t("Recipient phone"), key=f"chp{k['id']}")
            note = st.text_input(t("Note"), key=f"cho{k['id']}")
            if st.button(t("Send"), key=f"chgo{k['id']}", type="primary", disabled=kind == "RECIPIENT" and not (rname or "").strip()):
                E.request_change(k["id"], d.isoformat(), trip, kind, user["username"], new_stop, new_bus, rname, rphone, note)
                st.session_state.pp_last = ("ok", t("Request sent to the transport office."))
                st.rerun()
        with st.popover("🏠 " + t("Home location (pickup / drop-off)"), width="stretch"):
            for kind_, lab_ in (("PICKUP", "Pickup"), ("DROPOFF", "Drop-off")):
                cur_ = P.current_location(k["id"], kind_)
                same_ = " (same as pickup)" if cur_ and kind_ == "DROPOFF" and cur_.get("same_as_pickup") else ""
                st.markdown(f"**{lab_}{same_}:** " + ((cur_["address"] or "—") if cur_ and cur_["lat"] is not None else "not registered — please add"))
            pend_ = db.q1("SELECT COUNT(*) n FROM student_locations WHERE student_id=? AND status='PENDING'", (k["id"],))["n"]
            if pend_:
                st.info(f"{pend_} change(s) waiting for school approval.")
            st.caption("A permanent change of address needs school approval. The bus uses the nearest approved safe stop — never "
                       "your door. For one day only, use “Request a change”.")
            hk = st.segmented_control("For", ["Both (same place)", "Pickup", "Drop-off"], default="Both (same place)", key=f"hlk{k['id']}")
            ha = st.text_input("New address", key=f"hla{k['id']}", placeholder="Villa / building, street, zone")
            pin = st.text_input("Map pin (latitude, longitude)", key=f"hlp{k['id']}", placeholder="25.2850, 51.5100 — copy from Google Maps")
            hn = st.text_input("Notes (gate, landmark)", key=f"hln{k['id']}")
            hr = st.text_input(t("Reason"), key=f"hlr{k['id']}", placeholder="e.g. we moved house")
            if st.button(t("Send"), key=f"hlgo{k['id']}", type="primary", disabled=not (ha.strip() and pin.strip() and hr.strip())):
                try:
                    la_, lo_ = (float(v_) for v_ in pin.replace(" ", "").split(",")[:2])
                except ValueError:
                    la_ = lo_ = None
                if la_ is None or not P._valid_qatar(la_, lo_):
                    st.error("Please paste a valid map pin in Qatar, e.g. 25.2850, 51.5100")
                else:
                    if hk in ("Both (same place)", "Pickup"):
                        P.request_location(k["id"], "PICKUP", ha.strip(), la_, lo_, hn, gid, False, hr.strip(), user["username"], "guardian")
                    if hk == "Drop-off":
                        P.request_location(k["id"], "DROPOFF", ha.strip(), la_, lo_, hn, gid, False, hr.strip(), user["username"], "guardian")
                    elif hk == "Both (same place)":
                        P.request_location(k["id"], "DROPOFF", ha.strip(), None, None, "Same as pickup", gid, True, hr.strip(), user["username"], "guardian")
                    st.session_state.pp_last = ("ok", t("Request sent to the transport office.") + " The new location applies after school approval.")
                    st.rerun()
        with st.expander("Recent journeys & requests"):
            hist = db.q("""SELECT r.date AS Date, r.trip_no AS Trip, b.bus_no AS Bus, m.purpose AS Purpose, m.status AS Status,
                           substr(m.entry_ts,12,5) AS Boarded, substr(m.exit_ts,12,5) AS "Got off",
                           COALESCE(m.recipient_name, m.received_by, '') AS "Handed to / received by"
                           FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id WHERE m.student_id=?
                           ORDER BY r.date DESC, r.trip_no DESC LIMIT 20""", (k["id"],))
            for h in hist:
                h["Status"] = status_text(h["Status"])
            st.dataframe(hist, hide_index=True, width="stretch")
            reqs = db.q("""SELECT date AS "For date", COALESCE(trip_no,'All') AS Trip, kind AS Type, status AS Status FROM change_requests WHERE student_id=?
                           UNION ALL SELECT date, COALESCE(trip_no,'All'), 'ABSENCE', status FROM absences WHERE student_id=? ORDER BY 1 DESC LIMIT 10""",
                        (k["id"], k["id"]))
            if reqs:
                st.dataframe(reqs, hide_index=True, width="stretch")

st.subheader("✉️ " + t("Messages"))
notes = db.q("""SELECT n.*, s.name AS sname FROM notifications n LEFT JOIN students s ON s.id=n.student_id WHERE n.guardian_id=?
                ORDER BY n.ts DESC, n.id DESC LIMIT 200""", (gid,))
seen, inbox = set(), []
for n in notes:           # one row per message (in-app and e-mail copies of the same notice)
    key = (n["ts"], n["message"])
    if key in seen:
        continue
    seen.add(key)
    copies = [x for x in notes if (x["ts"], x["message"]) == key]
    inbox.append({"Time": n["ts"][:16], "Message": n["message"] + ("  🕓 delayed (bus was offline)" if n["delayed_offline"] else ""),
                  "In-app": next((x["status"] for x in copies if x["channel"] == "IN_APP"), "—"),
                  "E-mail": next((x["status"] + (f" ({x['attempts']} tries)" if x["attempts"] > 1 else "") for x in copies if x["channel"] == "EMAIL"), "—")})
if inbox:
    st.dataframe(inbox[:60], hide_index=True, width="stretch")
    if user["role"] == "guardian":
        db.x("UPDATE notifications SET is_read=1 WHERE guardian_id=? AND channel='IN_APP' AND is_read=0", (gid,))
else:
    st.caption("No messages yet.")
