"""Crew app (bus tablet): today's list, start with crew confirmation, explicit Check In / Check Out, handover, no-show,
offline queue + sync, encrypted roster, contingency roster, close with reconciliation and named sweep. Drivers: read-only."""
import json
import uuid
from datetime import date

import pandas as pd
import streamlit as st

import db
import device_store as dstore
import engine as E
import planner as P
import reports as R
from db import SCHOOL_STOP, TRIPS
from i18n import t
from qr_utils import decode_qr
from qr_scanner import qr_scanner
from ui import alert_sound, gps_fix, lines_table, msg, status_text

user = st.session_state.user
role = user["role"]
is_driver = role == "driver"
crew_role = role in ("supervisor", "caretaker", "driver")

# ------------------------------------------------------------------ which bus / device
if crew_role:
    me = db.q1("SELECT * FROM staff WHERE id=?", (user["staff_id"],))
    bus_id = me["bus_id"]
else:
    bl = db.buses()
    bus_id = st.selectbox(t("Bus"), [b["id"] for b in bl], format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i), key="crew_bus")
bus = db.bus(bus_id)
dev = db.q1("SELECT * FROM devices WHERE bus_id=? ORDER BY status='ACTIVE' DESC, id LIMIT 1", (bus_id,))
dev_id = dev["id"] if dev else None
today = db.today()

st.title(f"📷 {bus['bus_no']} — {t('Crew app')}")
_now = db.now()
st.markdown(f"<div class='datebar'>📅 <b>{_now:%A %d %b %Y}</b> · {_now:%H:%M} <span>Qatar</span></div>", unsafe_allow_html=True)
st.caption(f"{bus['route_name']} · {t('Driver')}: {db.staff_name(bus['driver_id'])} · {t('Supervisor')}: {db.staff_name(bus['supervisor_id'])} · "
           f"{t('Care-taker')}: {db.staff_name(bus['caretaker_id'])} · Device: {dev['name'] if dev else '—'}")

if dev and dev["status"] != "ACTIVE":
    msg("error", f"This device was revoked by the transport office ({dev['reason'] or 'no reason given'}). Scanning is disabled.")
    st.stop()
if is_driver:
    st.info(t("Drivers do not scan. This screen is read-only for the driver."))

K = f"b{bus_id}"
queue = dstore.load_queue(dev_id) if dev_id else []

with st.expander("📶 Device: connection & GPS", expanded=bool(queue)):
    c1, c2 = st.columns(2)
    offline = c1.toggle(t("Offline mode"), key=f"{K}_off")
    gps_off = c2.toggle(t("GPS unavailable"), key=f"{K}_gps")
    if queue:
        st.warning(f"{len(queue)} action(s) stored on this device (encrypted), waiting to sync.")
        if not offline and st.button(f"🔄 {t('Sync now')} ({len(queue)})", type="primary", disabled=is_driver):
            res = E.sync_queue(queue, user["username"], role)
            dstore.save_queue(dev_id, [])
            ok = sum(1 for _, r in res if r["ok"])
            conflicts = [r["message"] for _, r in res if r.get("conflict")]
            st.session_state[f"{K}_last"] = ("warn" if conflicts else "ok", f"Synced {ok}/{len(res)} action(s)."
                                             + (" Conflicts: " + " · ".join(conflicts) if conflicts else ""))
            st.rerun()
        if offline:
            st.caption("Turn offline mode off to sync.")


def replica(run_id, items, extra=None):
    """The device's local view: server state + queued offline actions (+ one new action), never committed."""
    con = db.connect()
    try:
        for it in items:
            if it["type"] == "event":
                E.process(it["run_id"], it["kind"], dict(it["ctx"]), badge_code=it.get("badge_code"), student_id=it.get("student_id"), con=con)
        res = None
        if extra:
            kind, ctx, badge, sid = extra
            res = E.process(run_id, kind, dict(ctx), badge_code=badge, student_id=sid, con=con)
        return res, E.run_lines(run_id, con)
    finally:
        con.rollback()
        con.close()


def nav_links(lat, lon, key):
    """Turn-by-turn navigation to a safe stop in Waze / Google Maps (set before moving — nothing to touch while driving)."""
    if lat is None or lon is None:
        return
    n1, n2 = st.columns(2)
    n1.link_button("🧭 Waze", f"https://waze.com/ul?ll={lat:.6f},{lon:.6f}&navigate=yes", width="stretch", key=f"{key}_wz")
    n2.link_button("🗺 Google Maps", f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=driving",
                   width="stretch", key=f"{key}_gm")


def route_stops(route, lines):
    """Stops in the order of the approved route (v2.0 planner); any other stop a child still needs is appended so nobody is lost."""
    slat, slon = db.school_pos()
    school = {"id": SCHOOL_STOP, "name": t("School"), "lat": slat, "lon": slon, "eta": None}
    out, seen = [school], {SCHOOL_STOP}
    for s_ in (route or {}).get("stops", []):
        if s_["stop_id"] == SCHOOL_STOP:   # boarding at school (start) vs. hand-over at school (end)
            boarding_left = any(ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED" for ln in lines)
            if s_["board"] and (boarding_left or school["eta"] is None):
                school["eta"] = s_["eta"]
            elif not s_["board"] and not boarding_left:
                school["eta"] = s_["eta"]
            continue
        if s_["stop_id"] in seen:
            continue
        seen.add(s_["stop_id"])
        out.append({"id": s_["stop_id"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "eta": s_["eta"]})
    extra = db.stops_of_bus(bus_id) + [db.stop(ln["stop_id"]) for ln in lines if ln.get("stop_id") not in seen]
    for s_ in extra:
        if s_ and s_["id"] not in seen:
            seen.add(s_["id"])
            out.append({"id": s_["id"], "name": s_["name"], "lat": s_["lat"], "lon": s_["lon"], "eta": None})
    return out


# ------------------------------------------------------------------ no open trip: today's list + start
run = E.open_run(bus_id)
closed_local = run and any(it["type"] == "close" and it["run_id"] == run["id"] for it in queue)

# ------------------------------------------------------------------ office messages + SOS (all crew, any time)
@st.fragment(run_every=15)
def _comms():
    msgs = E.bus_messages(bus_id, only_open=True)
    seen = st.session_state.setdefault(f"{K}_seenmsg", set())
    new = [m_ for m_ in msgs if m_["id"] not in seen]
    if new:   # refresh the whole screen (counts, stops) and announce the message
        st.session_state[f"{K}_say"] = "Message from the office. " + new[0]["text"]
        seen.update(m_["id"] for m_ in new)
        st.rerun()
    for m_ in msgs:
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.markdown(f"<div class='warn'>✉️ <b>{t('Message from office')}</b> · {m_['ts'][11:16]} · {m_['sender']}<br>{m_['text']}</div>",
                    unsafe_allow_html=True)
        if not is_driver and c2.button("✓ " + t("Acknowledge"), key=f"{K}_ack{m_['id']}", width="stretch", type="primary"):
            E.ack_bus_message(m_["id"], user["username"], role)
            st.rerun()
    for s_ in E.open_sos(bus_id):
        st.markdown(f"<div class='error'>🆘 <b>SOS sent {s_['ts'][11:16]}</b> — {t('the office has been alerted')}. {s_['details']}</div>",
                    unsafe_allow_html=True)


if st.session_state.get(f"{K}_say"):        # announcement queued by an auto-refresh (voice + chime)
    st.session_state[f"{K}_sayn"] = st.session_state.get(f"{K}_sayn", 0) + 1
    alert_sound(st.session_state.pop(f"{K}_say"), key=f"{K}_snd{st.session_state[f'{K}_sayn']}")
_comms()
with st.popover("🆘 SOS — " + t("Emergency"), width="stretch", type="primary"):
    st.markdown("**" + t("Send an emergency alert to the transport office") + "**")
    _sos_t = st.pills(t("Type"), E.SOS_TYPES, default="Medical", key=f"{K}_sostype") or "Other"
    _sos_n = st.text_input(t("Details (optional)"), key=f"{K}_sosnote", placeholder="e.g. child unwell, bus stopped at …")
    if offline:
        st.error("No network on this device — call the transport office directly.")
    if st.button("🆘 " + t("Send SOS now"), type="primary", width="stretch", key=f"{K}_sosgo", disabled=offline):
        _g = gps_fix(st.session_state.get(f"{K}_stop", SCHOOL_STOP), gps_off)
        E.raise_sos(bus_id, run["id"] if run else None, _sos_t, _sos_n.strip(), user["display_name"], role,
                    _g["lat"] if _g["gps_ok"] else None, _g["lon"] if _g["gps_ok"] else None)
        st.session_state[f"{K}_last"] = ("error", f"🆘 SOS ({_sos_t}) sent — the transport office has been alerted with your location.")
        st.rerun()

if f"{K}_last" in st.session_state and not run:
    msg(*st.session_state.pop(f"{K}_last"))

if not run:
    st.subheader(f"📋 Today's list — {today}")
    st.caption("The list of children expected on each trip is sent to the bus every day. Declared absences and approved stop/bus changes are "
               "already applied. Scan against this list: a child who is not on it is flagged as wrong bus.")
    runs_today = {r["trip_no"]: r for r in db.q("SELECT * FROM trip_runs WHERE bus_id=? AND date=?", (bus_id, today))}
    default_trip = E.trip_for_time()
    tabs = st.tabs([f"Trip {n}" + (" ✔" if n in runs_today else "") for n in TRIPS])
    for n, tab in zip(TRIPS, tabs):
        with tab:
            st.markdown(f"**{TRIPS[n]['label']}** · {TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]}")
            if n in runs_today:
                st.caption(f"Already run today ({runs_today[n]['status']}).")
                lines_table(E.run_lines(runs_today[n]["id"]))
                continue
            plan = E.manifest_for(bus_id, today, n)
            for ln in plan:
                ln["stop_name"] = db.stop(ln["stop_id"])["name"] if ln["stop_id"] is not None else ""
            mv = pd.DataFrame(plan)
            if not mv.empty:
                cnt = mv.groupby(["batch", "grp", "purpose"]).agg(Planned=("student_id", "count"),
                                                                  Absent=("status", lambda s: (s == "ABSENT_DECLARED").sum())).reset_index()
                st.dataframe(cnt.rename(columns={"batch": "Batch", "grp": "Group", "purpose": "Purpose"}), hide_index=True)
            lines_table(plan, ("name", "code", "class", "batch", "grp", "purpose", "stop_name", "status"))
            if plan:
                st.download_button(f"🖨 {t('Printable contingency roster')}", R.roster_pdf(plan, f"{bus['bus_no']} · {today} · Trip {n} roster"),
                                   file_name=f"{bus['bus_no']}_{today}_trip{n}_roster.pdf", mime="application/pdf", key=f"rost{n}")
    if dev_id:
        payload = {"bus": bus["bus_no"], "date": today, "trips": {n: E.manifest_for(bus_id, today, n) for n in TRIPS}}
        st.download_button(f"🔒 {t('Download encrypted roster')}", dstore.encrypted_roster(dev_id, payload),
                           file_name=f"{bus['bus_no']}_{today}_roster.enc", help="Encrypted with this device's key — unreadable if the tablet is lost.")

    if is_driver:
        # v2.0: the driver sees the route of the next trip before departure (read-only); the supervisor starts the trip
        _left = [n for n in TRIPS if n not in runs_today]
        if not _left:
            msg("ok", f"<b>{t('All trips for today are completed')}</b>")
            st.stop()
        _appr = [n for n in _left if P.active_plan(today, bus_id, n)]     # a trip the office has already approved a route for
        _nt = _appr[0] if _appr else (default_trip if default_trip in _left else _left[0])
        _act = P.active_plan(today, bus_id, _nt)
        _pv = json.loads(_act["plan_json"]) if _act else P.plan_trip(bus_id, today, _nt)
        st.divider()
        st.subheader("🗺 " + t("Route plan") + f" — Trip {_nt}")
        st.caption(f"{TRIPS[_nt]['label']} · {TRIPS[_nt]['window'][0]}–{TRIPS[_nt]['window'][1]} · "
                   + (f"approved route v{_act['version']}" if _act else "draft — approved when the supervisor starts the trip"))
        st.dataframe(pd.DataFrame([{"#": s_["seq"], "Stop": s_["name"], "Planned": s_["eta"], "Board": len(s_["board"]),
                                    "Get off": len(s_["exit"]), "On board after": s_["occ_after"]} for s_ in _pv["stops"]]),
                     hide_index=True, width="stretch")
        if _pv["flags"]:
            msg("warn", f"{len(_pv['flags'])} {t('location issue(s) before departure')} — "
                + t("These children use their assigned stop; the office has been shown the list."))
        st.info(t("Waiting for the supervisor to start the trip. This screen then shows the NEXT STOP by itself."))

        @st.fragment(run_every=10)
        def _wait_for_start():          # no tapping needed: switches to the trip screen as soon as the trip starts
            if E.open_run(bus_id):
                st.rerun()

        _wait_for_start()
        st.stop()
    st.divider()
    st.subheader("▶ " + t("Start trip"))
    avail = [n for n in TRIPS if n not in runs_today]
    if not avail:
        msg("ok", f"<b>{t('All trips for today are completed')}</b> — {len(runs_today)} / {len(TRIPS)} "
            + t("trips closed. Nothing more to start today."))
        st.stop()
    st.markdown("**" + t("Which trip are you starting?") + "**")
    trip_no = st.pills(t("Trip"), avail, default=default_trip if default_trip in avail else avail[0], key=f"{K}_trip_p",
                       format_func=lambda n: f"Trip {n} · {TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]}", label_visibility="collapsed") \
        or (default_trip if default_trip in avail else avail[0])
    st.markdown(f"<span class='pill'>{TRIPS[trip_no]['label']}</span>", unsafe_allow_html=True)
    if not E.in_window(trip_no, db.now()):
        st.caption(f"ℹ Outside the scheduled window ({TRIPS[trip_no]['window'][0]}–{TRIPS[trip_no]['window'][1]}). It will show on the punctuality report.")
    # v2.0: the planned route for this trip and the pre-departure location check
    _act = P.active_plan(today, bus_id, trip_no)
    _dr = P.draft_plan(today, bus_id, trip_no)
    _pv = json.loads(_act["plan_json"]) if _act else P.plan_trip(bus_id, today, trip_no)
    with st.container(border=True):
        st.markdown("**🗺 " + t("Route plan") + "** — " + (f"approved v{_act['version']}" if _act else
                    "not approved yet · generated and activated when the trip starts"))
        st.caption(" → ".join(f"{s_['name']} {s_['eta']}" for s_ in _pv["stops"]) or "No stops")
        if _pv["flags"]:
            msg("warn", f"<b>{len(_pv['flags'])} {t('location issue(s) before departure')}</b> — "
                + ", ".join(f"{f_['name']} ({f_['issue'].split(' —')[0].lower()})" for f_ in _pv["flags"][:6])
                + ". " + t("These children use their assigned stop; the office has been shown the list."))
        if _pv["violations"]:
            msg("error", "Plan problem: " + "; ".join(_pv["violations"]) + " — call the transport office.")
        if _dr and role == "supervisor":
            _pd = json.loads(_dr["plan_json"])
            st.markdown(f"Draft v{_dr['version']} waiting for approval: *{_dr['reason']}*")
            if st.button("✅ Approve route", key=f"{K}_preok", disabled=bool(_pd["violations"])):
                _ok, _m = P.approve_plan(_dr["id"], user["username"], role)
                msg("ok" if _ok else "error", _m)

    def crew_pick(label, r, default):
        opts = db.q("SELECT id, name FROM staff WHERE role=? ORDER BY bus_id=? DESC, name", (r, bus_id))
        ids = [o["id"] for o in opts]
        return st.selectbox(label, ids, index=ids.index(default) if default in ids else 0,
                            format_func=lambda i: next(o["name"] for o in opts if o["id"] == i), key=f"{K}_{r}")

    with st.expander(f"👥 {t('Crew')}: {db.staff_name(bus['driver_id'])} · {db.staff_name(bus['supervisor_id'])} · "
                     f"{db.staff_name(bus['caretaker_id'])} — {t('tap to change')}"):
        d_id = crew_pick(t("Driver"), "driver", bus["driver_id"])
        s_id = crew_pick(t("Supervisor"), "supervisor", bus["supervisor_id"])
        c_id = crew_pick(t("Care-taker"), "caretaker", bus["caretaker_id"])
    conf = st.checkbox(t("Confirm crew on board"), key=f"{K}_conf")
    if st.button("▶ " + t("Start trip"), type="primary", disabled=not conf or offline, width="stretch"):
        first = SCHOOL_STOP if any(m[2] == "DROPOFF" for m in TRIPS[trip_no]["movements"]) else (db.stops_of_bus(bus_id) or [{"id": SCHOOL_STOP}])[0]["id"]
        g = gps_fix(first, gps_off)
        rid, m = E.start_run(bus_id, today, trip_no, d_id, s_id, c_id, g["lat"], g["lon"], user["username"], role, dev_id)
        if not rid:
            msg("error", m)          # e.g. this trip already ran today — show it here, next to the button
        else:
            try:                     # v2.0: the approved route goes with the trip (or is generated and approved now)
                _a = P.attach_to_started_run(E.get_run(rid), user["username"], role)
                if _a:
                    m += f" Route v{_a['version']} active."
                    if dev_id:
                        dstore.save_route(dev_id, {"run_id": rid, "version": _a["version"], "plan": json.loads(_a["plan_json"]), "saved": db.ts()})
                else:
                    m += " Route waiting for supervisor approval — stops follow the usual order meanwhile."
            except Exception as ex:  # never block a trip start on planning
                m += f" (route plan unavailable: {ex})"
            st.session_state[f"{K}_last"] = ("ok", m + ("" if g["gps_ok"] else " (start location missing — GPS unavailable)"))
            st.rerun()
    if offline:
        st.caption("Trips are started online (the device downloads the day's manifest). Scanning then continues offline.")
    st.stop()

# ------------------------------------------------------------------ trip in progress
lines = replica(run["id"], queue)[1] if queue else E.run_lines(run["id"])
# v2.0: approved route. Online: fetch (after checking for approved changes) and keep a copy on the device. Offline: last saved copy.
route, route_ver, route_note, route_draft = None, None, "", None
if offline:
    _c = dstore.load_route(dev_id) if dev_id else None
    if _c and _c.get("run_id") == run["id"]:
        route, route_ver = _c["plan"], _c["version"]
        route_note = f"📴 Offline — using the last approved route saved on this device (v{route_ver}, saved {str(_c['saved'])[11:16]})."
    else:
        route_note = "📴 Offline — no saved route on this device; stops follow the usual order."
else:
    try:
        route_draft = None if is_driver else P.check_replan(run["date"], bus_id, run["trip_no"], user["username"], role)
        _act = P.active_plan(run["date"], bus_id, run["trip_no"])
        route_draft = route_draft or P.draft_plan(run["date"], bus_id, run["trip_no"])
        if _act:
            route, route_ver = json.loads(_act["plan_json"]), _act["version"]
            if dev_id:
                _c = dstore.load_route(dev_id)
                if not _c or _c.get("run_id") != run["id"] or _c.get("version") != route_ver:
                    dstore.save_route(dev_id, {"run_id": run["id"], "version": route_ver, "plan": route, "saved": db.ts()})
    except Exception as ex:
        route_note = f"Route plan unavailable ({ex}) — stops follow the usual order."
cnt = pd.Series([ln["status"] for ln in lines]).value_counts().to_dict() if lines else {}
_th1, _th2 = st.columns([5, 1], vertical_alignment="center")
_th1.markdown(f"### Trip {run['trip_no']} · {TRIPS[run['trip_no']]['label']}")
if _th2.button("🔄 " + t("Refresh"), key=f"{K}_refresh_top", width="stretch",
               help="Reload counts, lists and the close-trip check (shows scans made on other phones too)"):
    st.rerun()
_rd = date.fromisoformat(run["date"])
if run["date"] != today:
    msg("error", f"{t('This trip is from')} <b>{_rd:%A %d %b %Y}</b> — "
        + t("it was never closed. Account for every child and close it, or call the transport office."))
st.caption(f"📅 {_rd:%a %d %b %Y} · Started {run['start_ts'][11:16]} by {run['started_by']} · crew: {db.staff_name(run['driver_id'])} / {db.staff_name(run['supervisor_id'])} / "
           f"{db.staff_name(run['caretaker_id'])}" + (" · 📴 OFFLINE" if offline else ""))
_cn = [(t("On board"), cnt.get("ONBOARD", 0) + cnt.get("TRANSFER_PENDING", 0), "#2E9E5B"), (t("Expected"), cnt.get("EXPECTED", 0), "#E07A1F"),
       (t("Completed"), cnt.get("COMPLETED", 0) + cnt.get("RETURNED", 0), "#3D4FD6"),
       (t("Absent"), cnt.get("ABSENT_DECLARED", 0) + cnt.get("NO_SHOW", 0), "#6B7390")]
st.markdown("<div class='crewcnt'>" + "".join(f"<div><b style='color:{c}'>{v}</b><span>{l}</span></div>" for l, v, c in _cn) + "</div>",
            unsafe_allow_html=True)

# all assigned students boarded -> banner + one voice announcement per trip (crew and driver screens)
_onb = cnt.get("ONBOARD", 0) + cnt.get("TRANSFER_PENDING", 0)
_exp_drop = sum(1 for ln in lines if ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED")
_has_drop = any(ln["purpose"] == "DROPOFF" for ln in lines)
if cnt.get("EXPECTED", 0) == 0 and _onb:
    msg("ok", f"<b>✅ {t('All students have boarded')}</b> — {_onb} {t('on board, no one left to pick up.')}")
    if not st.session_state.get(f"{K}_allb_{run['id']}"):
        st.session_state[f"{K}_allb_{run['id']}"] = True
        st.session_state[f"{K}_schb_{run['id']}"] = True
        st.session_state[f"{K}_sayn"] = st.session_state.get(f"{K}_sayn", 0) + 1
        alert_sound("All students have boarded the bus.", key=f"{K}_snd{st.session_state[f'{K}_sayn']}")
elif _has_drop and _exp_drop == 0 and _onb:
    # combined trip: everyone going home is on board at school (shown until the first child gets off at a stop)
    if not any(ln["purpose"] == "DROPOFF" and ln["status"] in ("COMPLETED", "RETURNED") for ln in lines):
        msg("ok", f"<b>✅ {t('All drop-off students are on board')}</b> — {t('the bus can leave school.')}")
    if not st.session_state.get(f"{K}_schb_{run['id']}"):
        st.session_state[f"{K}_schb_{run['id']}"] = True
        st.session_state[f"{K}_sayn"] = st.session_state.get(f"{K}_sayn", 0) + 1
        alert_sound("All drop-off students are on board. The bus can leave school.", key=f"{K}_snd{st.session_state[f'{K}_sayn']}")

if is_driver and not offline:
    @st.fragment(run_every=8)
    def _driver_watch():
        """Driver screen refreshes by itself: any scan, absence, route change or office message reloads counts, lists and highlights."""
        sig = (tuple(sorted((r["status"], r["n"]) for r in db.q("SELECT status, COUNT(*) n FROM manifest WHERE run_id=? GROUP BY status",
                                                                 (run["id"],)))),
               (db.q1("SELECT MAX(id) m FROM events WHERE run_id=?", (run["id"],)) or {}).get("m"),
               (db.q1("SELECT MAX(id) m FROM bus_messages WHERE bus_id=?", (bus_id,)) or {}).get("m") if db.q1(
                   "SELECT name FROM sqlite_master WHERE name='bus_messages'") else None,
               (P.active_plan(run["date"], bus_id, run["trip_no"]) or {}).get("version"),
               bool(E.open_run(bus_id)))
        prev = st.session_state.get(f"{K}_dsig")
        st.session_state[f"{K}_dsig"] = sig
        if prev is not None and prev != sig:
            st.rerun()

    _driver_watch()

if not is_driver and not closed_local and not db.q1("SELECT id FROM events WHERE run_id=? AND result<>'REJECTED' "
                                                     "AND kind IN ('CHECK_IN','CHECK_OUT') LIMIT 1", (run["id"],)):
    with st.expander("↩ Wrong trip? Cancel it (possible until the first child is checked in)", expanded=True):
        st.caption("This trip carries: " + ", ".join(f"Batch {b} {g} {p.lower().replace('dropoff', 'drop-off')}"
                                                     for b, g, p in TRIPS[run["trip_no"]]["movements"]))
        if st.button("Cancel this trip", key=f"{K}_cancel"):
            ok, m = E.cancel_run(run["id"], user["username"], role)
            st.session_state[f"{K}_last"] = ("ok" if ok else "error", m)
            st.rerun()

if closed_local:
    msg("warn", "Trip closed on this device — <b>Pending Sync</b>. The server confirms closure only after the queued scans are synced and "
        "validated. Turn offline mode off and press Sync.")
    st.stop()

if f"{K}_last" in st.session_state:
    msg(*st.session_state[f"{K}_last"])
if route_note:
    st.caption(route_note)
if route_draft and not is_driver:
    _pd = json.loads(route_draft["plan_json"])
    with st.container(border=True):
        st.markdown(f"🔁 **{t('Revised route waiting for approval')}** (v{route_draft['version']}) — {route_draft['reason']}")
        st.caption(t("Remaining stops") + ": " + " → ".join(f"{s_['name']} {s_['eta']}" for s_ in _pd["stops"] if not s_.get("frozen")))
        if _pd["violations"]:
            msg("error", "Cannot be activated: " + "; ".join(_pd["violations"]))
        if role == "supervisor":
            _a1, _a2 = st.columns(2)
            if _a1.button("✅ " + t("Approve revised route"), type="primary", key=f"{K}_rvok", width="stretch", disabled=bool(_pd["violations"])):
                _ok, _m = P.approve_plan(route_draft["id"], user["username"], role)
                st.session_state[f"{K}_last"] = ("ok" if _ok else "error", _m)
                st.rerun()
            if _a2.button(t("Keep current route"), key=f"{K}_rvno", width="stretch"):
                P.reject_plan(route_draft["id"], user["username"], role, "kept current route")
                st.rerun()
        else:
            st.caption(t("The supervisor or the transport office approves route changes."))


def stop_work(lines, stops):
    """Per stop: children to board (on) and to get off / hand over (off) still outstanding."""
    w = {}
    for s_ in stops:
        sid_ = s_["id"]
        if sid_ == SCHOOL_STOP:
            on = sum(1 for ln in lines if ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED")
            off = sum(1 for ln in lines if ln["purpose"] == "PICKUP" and ln["status"] == "ONBOARD")
        else:
            on = sum(1 for ln in lines if ln["purpose"] == "PICKUP" and ln["status"] == "EXPECTED" and ln["stop_id"] == sid_)
            off = sum(1 for ln in lines if ln["purpose"] == "DROPOFF" and ln["status"] == "ONBOARD" and ln["stop_id"] == sid_)
        w[sid_] = {"on": on, "off": off, "any": bool(on or off)}
    return w


def next_stop(lines, stops, work):
    """Suggested next stop: board drop-off children at school first, then route stops in order, then school hand-over."""
    if work[SCHOOL_STOP]["on"]:
        return SCHOOL_STOP
    for s_ in stops:
        if s_["id"] != SCHOOL_STOP and work[s_["id"]]["any"]:
            return s_["id"]
    if work[SCHOOL_STOP]["off"]:
        return SCHOOL_STOP
    return None


def suggest_action(stop_id, work):
    w = work.get(stop_id, {"on": 0, "off": 0})
    return t("Check Out") if w["off"] and not (stop_id == SCHOOL_STOP and w["on"]) else t("Check In")


def _work_text(w):
    parts = []
    if w["off"]:
        parts.append(f"⬇ {w['off']} {t('to get off')}")
    if w["on"]:
        parts.append(f"⬆ {w['on']} {t('to board')}")
    return " · ".join(parts) or t("nothing to do here")


def submit(kind, badge=None, sid=None, method="QR", extra=None, ctx=None, pend_id=None):
    """Send one crew action to the server (or to the device queue when offline)."""
    if ctx is None:
        ctx = {"operator": user["display_name"], "role": role, "device_id": dev_id, "stop_id": st.session_state.get(f"{K}_stop", SCHOOL_STOP),
               "method": method, "captured_ts": db.ts(), "client_uuid": str(uuid.uuid4())}
        ctx.update(gps_fix(ctx["stop_id"], gps_off))
        if offline:
            ctx["offline"] = True
            ctx["sync_state"] = "PENDING"
    ctx.update(extra or {})
    if offline:
        res, _ = replica(run["id"], queue, (kind, ctx, badge, sid))
        if res["ok"] and not res.get("needs"):
            queue.append({"type": "event", "run_id": run["id"], "kind": kind, "ctx": ctx, "badge_code": badge, "student_id": sid})
            dstore.save_queue(dev_id, queue)
            res["message"] += " · 📴 saved on device, will sync"
    else:
        res = E.process(run["id"], kind, ctx, badge_code=badge, student_id=sid)
    # several scans can wait for confirmation at the same time — a new scan never drops an unconfirmed one
    pl = st.session_state.setdefault(f"{K}_pendings", [])
    if pend_id is not None and not res.get("needs") and "Over-capacity needs" not in res["message"]:
        pl[:] = [x_ for x_ in pl if x_["id"] != pend_id]
    if res.get("needs") or "Over-capacity needs" in res["message"]:
        sid_ = res.get("student_id") or sid
        item = {"id": pend_id or str(uuid.uuid4()), "kind": kind, "badge": badge, "sid": sid, "ctx": ctx,
                "needs": res.get("needs") or "capacity", "allowed": res.get("allowed"), "student_id": sid_,
                "name": res.get("student_name") or (db.q1("SELECT name FROM students WHERE id=?", (sid_,)) or {}).get("name", "Student")}
        pl[:] = [x_ for x_ in pl if not (sid_ and x_.get("student_id") == sid_)] + [item]
    st.session_state[f"{K}_last"] = (res["level"], res["message"])


if is_driver:
    @st.fragment(run_every=10)
    def _driver_next():
        if not offline and not E.open_run(bus_id):     # trip closed by the supervisor: back to today's list automatically
            st.rerun()
        _l = replica(run["id"], dstore.load_queue(dev_id))[1] if dev_id and dstore.load_queue(dev_id) else E.run_lines(run["id"])
        _stops = route_stops(route, _l)
        _w = stop_work(_l, _stops)
        _n = next_stop(_l, _stops, _w)
        _name = {s_["id"]: s_["name"] for s_ in _stops}
        _byid = {s_["id"]: s_ for s_ in _stops}
        _eta = _byid[_n]["eta"] if _n is not None else None
        _eta_html = (f"<div style='font-size:.95rem;margin-top:6px;opacity:.9'>{t('Planned')} {_eta}"
                     + (f" · route v{route_ver}" if route_ver else "") + "</div>") if _eta else ""
        if _n is None:
            big, sub, col = t("Back to school / trip complete"), t("All stops done"), "#2E9E5B"
        else:
            big, sub, col = _name[_n], _work_text(_w[_n]), "#3D4FD6"
        st.markdown(f"""<div style='background:{col};color:#fff;border-radius:16px;padding:18px 20px;margin:6px 0 12px'>
<div style='font-size:.9rem;opacity:.85;letter-spacing:.06em'>{t('NEXT STOP')}</div>
<div style='font-size:2.2rem;font-weight:800;line-height:1.15'>{big}</div>
<div style='font-size:1.1rem;margin-top:4px'>{sub}</div>{_eta_html}</div>""", unsafe_allow_html=True)
        if _n is not None:
            nav_links(_byid[_n]["lat"], _byid[_n]["lon"], f"{K}_dnav")
            st.caption(t("Start navigation before moving — the screen and voice update by themselves, nothing to touch while driving."))
        _order = ([SCHOOL_STOP] if _w[SCHOOL_STOP]["on"] else []) + \
                 [s_["id"] for s_ in _stops if s_["id"] != SCHOOL_STOP and _w[s_["id"]]["any"]] + \
                 ([SCHOOL_STOP] if _w[SCHOOL_STOP]["off"] else [])
        _after = [i for i in _order if i != _n] if _order and _order[0] == _n else [i for i in _order if i != _n]
        if _after:
            st.caption(t("Then") + ": " + " → ".join(_name[i] for i in dict.fromkeys(_after)))
        if st.session_state.get(f"{K}_drvnext", "init") != _n:
            _first = st.session_state.get(f"{K}_drvnext", "init") == "init"
            st.session_state[f"{K}_drvnext"] = _n
            if not _first:   # stop finished: refresh the whole screen (counts, lists) and announce the next stop
                st.session_state[f"{K}_say"] = f"Next stop, {big}" if _n is not None else "All stops done"
                st.rerun()

    _driver_next()

if not is_driver:
    stops = route_stops(route, lines)
    work = stop_work(lines, stops)
    nxt = next_stop(lines, stops, work)
    if f"{K}_stop" not in st.session_state:
        st.session_state[f"{K}_stop"] = nxt or SCHOOL_STOP
    if f"{K}_act" not in st.session_state:
        st.session_state[f"{K}_act"] = suggest_action(st.session_state[f"{K}_stop"], work)
    st.session_state[f"{K}_stopp"] = st.session_state[f"{K}_stop"]
    _c = st.session_state[f"{K}_stop"]
    _sig = (_c, work[_c]["off"], work[_c]["on"])
    if st.session_state.get(f"{K}_worksig") not in (None, _sig) and st.session_state.get(f"{K}_worksig")[0] == _c:
        # counts at this stop changed (a scan landed): flip the action when one direction is finished
        if work[_c]["off"] == 0 and work[_c]["on"]:
            st.session_state[f"{K}_act"] = t("Check In")
        elif work[_c]["on"] == 0 and work[_c]["off"]:
            st.session_state[f"{K}_act"] = t("Check Out")
    st.session_state[f"{K}_worksig"] = _sig

    def _go(stop_id):
        st.session_state[f"{K}_stop"] = stop_id
        st.session_state[f"{K}_act"] = suggest_action(stop_id, work)

    def _pick_stop():
        v = st.session_state.get(f"{K}_stopp")
        if v is not None:
            _go(v)

    cur = st.session_state[f"{K}_stop"]
    name_of = {s["id"]: s["name"] for s in stops}
    with st.container(border=True):
        if nxt is None:
            st.markdown(f"✅ **{t('All stops done')}** — {t('sweep the bus and close the trip below.')}")
        elif nxt != cur:
            w = work[nxt]
            pc1, pc2 = st.columns([3, 2], vertical_alignment="center")
            _neta = next((s_["eta"] for s_ in stops if s_["id"] == nxt), None)
            pc1.markdown(f"👉 **{t('Next stop')}: {name_of[nxt]}** — " + _work_text(w) + (f" · {t('Planned')} {_neta}" if _neta else ""))
            pc2.button(f"➡ {t('Go to')} {name_of[nxt]}", type="primary", width="stretch", on_click=_go, args=(nxt,), key=f"{K}_gonext")
            _ns = next(s_ for s_ in stops if s_["id"] == nxt)
            nav_links(_ns["lat"], _ns["lon"], f"{K}_cnav")
        else:
            st.markdown(f"📍 **{t('You are at')} {name_of[cur]}** — " + (_work_text(work[cur]) if work[cur]["any"] else t("nothing to do here")))
        st.pills(t("Current stop"), [s["id"] for s in stops], key=f"{K}_stopp", on_change=_pick_stop,
                 format_func=lambda i: f"{'✓ ' if not work[i]['any'] else ''}{name_of[i]}" + (f"  ↓{work[i]['off']}" if work[i]["off"] else "")
                 + (f"  ↑{work[i]['on']}" if work[i]["on"] else ""))
        action = st.segmented_control(t("Action"), [t("Check In"), t("Check Out")], key=f"{K}_act", width="stretch",
                                      format_func=lambda a: ("⬆ " if a == t("Check In") else "⬇ ") + a) or t("Check In")
    kind = "CHECK_IN" if action == t("Check In") else "CHECK_OUT"

    _pl = st.session_state.get(f"{K}_pendings", [])
    if _pl:
        st.markdown(f"<div class='warn'>⏳ <b>{len(_pl)} {t('waiting for confirmation')}</b> — "
                    + t("the child is NOT checked out until you confirm. Confirm each card below.") + "</div>", unsafe_allow_html=True)
    _stopname = {s_["id"]: s_["name"] for s_ in stops}
    for pend in list(_pl):
        pid = pend["id"]
        with st.container(border=True):
            st.markdown(f"**{pend['name']}** · {'Check Out' if pend['kind'] == 'CHECK_OUT' else 'Check In'} at "
                        f"{_stopname.get(pend['ctx'].get('stop_id'), t('School'))} · {t('scanned')} {pend['ctx']['captured_ts'][11:16]}")
            if pend["needs"] == "recipient":
                st.caption(t("Handed to (authorized recipient)"))
                opts = [a["name"] for a in pend["allowed"] or []] + ["__NONE__"]
                lab = {a["name"]: f"{a['name']} — {a['relation']} · {a['phone']}" for a in pend["allowed"] or []}
                lab["__NONE__"] = "⛔ " + t("No approved recipient present")
                pick = st.radio("Recipient", opts, format_func=lab.get, label_visibility="collapsed", key=f"{K}_rec_{pid}")
                if st.button("✅ " + t("Confirm handover"), type="primary", key=f"{K}_cf_{pid}", width="stretch"):
                    submit(pend["kind"], pend["badge"], pend["sid"], ctx=pend["ctx"], extra={"recipient": pick}, pend_id=pid)
                    st.rerun()
            elif pend["needs"] == "receiver":
                st.caption(t("Received by (school staff)"))
                rec = [r["name"] for r in db.q("SELECT name FROM staff WHERE role='receiving' ORDER BY name")]
                pick = st.pills("Receiver", rec, default=rec[0] if rec else None, label_visibility="collapsed", key=f"{K}_rcv_{pid}") or (rec[0] if rec else None)
                if st.button("✅ " + t("Confirm handover"), type="primary", key=f"{K}_cf_{pid}", width="stretch"):
                    submit(pend["kind"], pend["badge"], pend["sid"], ctx=pend["ctx"], extra={"received_by": pick}, pend_id=pid)
                    st.rerun()
            elif pend["needs"] == "capacity":
                st.markdown("**Bus is full — supervisor override**")
                why = st.text_input(t("Reason"), key=f"{K}_capwhy_{pid}")
                if st.button("Override and check in", disabled=not why.strip(), key=f"{K}_cap_{pid}"):
                    submit(pend["kind"], pend["badge"], pend["sid"], ctx=pend["ctx"], extra={"capacity_override": why.strip()}, pend_id=pid)
                    st.rerun()
            if st.button(t("Cancel this scan"), key=f"{K}_cx_{pid}"):
                st.session_state[f"{K}_pendings"] = [x_ for x_ in _pl if x_["id"] != pid]
                st.rerun()

    # one child at a time: who the crew should scan next at this stop
    _cs = st.session_state.get(f"{K}_stop", SCHOOL_STOP)
    _off = sorted([ln for ln in lines if (ln["purpose"] == "DROPOFF" and ln["status"] == "ONBOARD" and ln["stop_id"] == _cs) or
                   (_cs == SCHOOL_STOP and ln["purpose"] == "PICKUP" and ln["status"] == "ONBOARD")], key=lambda ln: ln["name"])
    _on = sorted([ln for ln in lines if (ln["purpose"] == "PICKUP" and ln["status"] == "EXPECTED" and ln["stop_id"] == _cs) or
                  (_cs == SCHOOL_STOP and ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED")], key=lambda ln: ln["name"])
    _busy = {p_.get("student_id") for p_ in _pl}
    _off = [ln for ln in _off if ln["student_id"] not in _busy]
    _on = [ln for ln in _on if ln["student_id"] not in _busy]
    _prim, _verb, _other = (_off, t("Check Out"), _on) if kind == "CHECK_OUT" else (_on, t("Check In"), _off)
    _sname = name_of.get(_cs, t("School"))
    if _prim:
        _n1 = _prim[0]
        _then = (" · " + t("then") + f" {_prim[1]['name']}") if len(_prim) > 1 else ""
        st.markdown(f"""<div style='border:2px solid #3D4FD6;background:#EEF0FD;border-radius:14px;padding:12px 16px;margin:6px 0 10px'>
<div style='font-size:.8rem;color:#3D4FD6;font-weight:700;letter-spacing:.05em'>{t('NEXT STUDENT')} · {"⬇" if kind == "CHECK_OUT" else "⬆"} {_verb}</div>
<div style='font-size:1.55rem;font-weight:800;color:#1A2036;line-height:1.2'>{_n1['name']}</div>
<div style='font-size:.95rem;color:#3a4160'>{_n1['class'] or ''} · {_n1['code']} · {_sname}</div>
<div style='font-size:.8rem;color:#6B7390;margin-top:3px'>1 {t('of')} {len(_prim)} {t('at this stop')}{_then}</div></div>""",
                    unsafe_allow_html=True)
    elif _other:
        st.markdown(f"<div class='warn'>{t('Nobody left to')} {_verb.lower()} {t('here')} — {len(_other)} "
                    f"{t('to') } {(t('Check In') if kind == 'CHECK_OUT' else t('Check Out')).lower()}. {t('Switch the action above.')}</div>",
                    unsafe_allow_html=True)
    elif work.get(_cs, {}).get("any") is False and nxt is not None:
        st.markdown(f"<div class='ok'>✓ {t('Nothing more at')} {_sname}. {t('Next stop')}: <b>{name_of[nxt]}</b></div>", unsafe_allow_html=True)

    tab_cam, tab_type, tab_man, tab_photo = st.tabs(["📷 " + t("Scan badge with camera"), "⌨️ " + t("Badge reader / type code"),
                                                     "✍️ " + t("Manual entry"), "📸 Photo scan"])
    with tab_cam:
        st.caption("Uses the BACK camera and scans continuously — hold the badge inside the square; it is recorded automatically "
                   "(beep). Set the stop and Check In / Check Out first. Allow camera access the first time.")
        val = qr_scanner(key=f"{K}_live")
        if val and val.get("nonce") and val["nonce"] != st.session_state.get(f"{K}_nonce"):
            st.session_state[f"{K}_nonce"] = val["nonce"]
            submit(kind, badge=val["code"], method="QR")
            st.rerun()
    with tab_photo:
        st.caption("Backup: take a photo of the badge (if the live scanner does not start on this device).")
        shot = st.camera_input("QR", label_visibility="collapsed", key=f"{K}_cam{st.session_state.get(f'{K}_camn', 0)}")
        if shot is not None:
            code = decode_qr(shot.getvalue())
            st.session_state[f"{K}_camn"] = st.session_state.get(f"{K}_camn", 0) + 1
            if code:
                submit(kind, badge=code, method="QR")
            else:
                st.session_state[f"{K}_last"] = ("error", "No QR code found — hold the badge closer and try again.")
            st.rerun()
    with tab_type:
        with st.form(f"{K}_typed", clear_on_submit=True):
            code = st.text_input(t("Badge code"), placeholder="Scan with a USB/Bluetooth reader or type the code")
            if st.form_submit_button(t("Submit"), type="primary") and code.strip():
                submit(kind, badge=code.strip(), method="READER")
                st.rerun()
    with tab_man:
        st.caption("Only when the badge is missing or unreadable. Needs a reason and a second crew member who verified the child.")
        opts = {ln["student_id"]: f"{ln['name']} · {ln['class']} · {ln['stop_name'] or t('School')} · {status_text(ln['status'])}" for ln in lines}
        allst = db.q("SELECT id, name, code FROM students WHERE active=1 ORDER BY name")
        for s in allst:
            opts.setdefault(s["id"], f"{s['name']} ({s['code']}) — not on this list")
        sid = st.selectbox(t("Student"), list(opts), format_func=opts.get, index=None, placeholder="Search name…", key=f"{K}_mansid")
        _why_opts = ["Badge forgotten", "Badge damaged / unreadable", "Camera / reader fault", "Paper roster entry",
                     "Handover not confirmed — child already left the bus"]
        why = st.pills(t("Reason"), _why_opts, default=_why_opts[0], key=f"{K}_manwhy") or _why_opts[0]
        crew_names = [db.staff_name(run["supervisor_id"]), db.staff_name(run["caretaker_id"]), db.staff_name(run["driver_id"])]
        _ver_opts = [n for n in crew_names if n and n != user["display_name"]] or crew_names
        ver = st.pills(t("Verified by"), _ver_opts, default=_ver_opts[0], key=f"{K}_manver") or _ver_opts[0]
        if st.button(t("Submit"), disabled=sid is None, key=f"{K}_mango"):
            submit(kind, sid=sid, method="MANUAL", extra={"manual_reason": why, "verified_by": ver})
            st.rerun()

cur_stop = st.session_state.get(f"{K}_stop", SCHOOL_STOP)
_arrive = [ln for ln in lines if ln["status"] == "ONBOARD" and ln["purpose"] == "PICKUP"]
if not is_driver and _arrive and cur_stop == SCHOOL_STOP:
    with st.container(border=True):
        st.markdown(f"**🏫 Arrived at school — hand over {len(_arrive)} pickup child(ren) to receiving staff**")
        st.caption("Records a separate school check-out for every child, with the receiving staff member's name. "
                   "Children still on a drop-off route are not affected.")
        _rcv = [r["name"] for r in db.q("SELECT name FROM staff WHERE role='receiving' ORDER BY name")]
        _who = st.pills(t("Received by (school staff)"), _rcv, default=_rcv[0] if _rcv else None, key=f"{K}_bulkrcv") or (_rcv[0] if _rcv else None)
        if st.button(f"✅ Hand over all {len(_arrive)}", type="primary", key=f"{K}_bulkgo", width="stretch"):
            _ok = 0
            for ln in _arrive:
                _ctx = {"operator": user["display_name"], "role": role, "device_id": dev_id, "stop_id": SCHOOL_STOP, "method": "GROUP_HANDOVER",
                        "captured_ts": db.ts(), "client_uuid": str(uuid.uuid4()), "received_by": _who, **gps_fix(SCHOOL_STOP, gps_off)}
                if offline:
                    _ctx.update(offline=True, sync_state="PENDING")
                    queue.append({"type": "event", "run_id": run["id"], "kind": "CHECK_OUT", "ctx": _ctx, "badge_code": None, "student_id": ln["student_id"]})
                    _ok += 1
                else:
                    _ok += bool(E.process(run["id"], "CHECK_OUT", _ctx, student_id=ln["student_id"])["ok"])
            if offline:
                dstore.save_queue(dev_id, queue)
            st.session_state[f"{K}_last"] = ("ok", f"{_ok} child(ren) handed over at school to {_who}." + (" · 📴 saved on device, will sync" if offline else ""))
            st.rerun()

st.divider()
_auto = st.toggle("🔄 Auto-refresh lists every 10 s (shows scans made on other phones too)", value=True, key=f"{K}_auto")


@st.fragment(run_every=10 if _auto else None)
def _lists_panel():
    cur_stop = st.session_state.get(f"{K}_stop", SCHOOL_STOP)
    if is_driver:          # the driver does not pick a stop: highlight the children at the NEXT STOP
        _dn = st.session_state.get(f"{K}_drvnext")
        cur_stop = _dn if _dn not in (None, "init") else SCHOOL_STOP
    _q = dstore.load_queue(dev_id) if dev_id else []
    _lines = replica(run["id"], _q)[1] if _q else E.run_lines(run["id"])
    _cnt = pd.Series([ln["status"] for ln in _lines]).value_counts().to_dict() if _lines else {}
    h1, h2 = st.columns([1, 3])
    if h1.button("🔄 Refresh", key=f"{K}_refresh", help="Reload the whole screen: counts, lists and the close-trip check"):
        st.rerun()
    h2.caption(f"Updated {db.now():%H:%M:%S} · on board {_cnt.get('ONBOARD', 0) + _cnt.get('TRANSFER_PENDING', 0)} · "
               f"expected {_cnt.get('EXPECTED', 0)} · completed {_cnt.get('COMPLETED', 0) + _cnt.get('RETURNED', 0)}")
    _up = []
    for _s in route_stops(route, _lines):
        _pick = sum(1 for ln in _lines if ln["purpose"] == "PICKUP" and ln["status"] == "EXPECTED" and ln["stop_id"] == _s["id"])
        _board = sum(1 for ln in _lines if ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED") if _s["id"] == SCHOOL_STOP else 0
        _drop = sum(1 for ln in _lines if ln["purpose"] == "DROPOFF" and ln["status"] == "ONBOARD" and ln["stop_id"] == _s["id"])
        _hand = sum(1 for ln in _lines if ln["purpose"] == "PICKUP" and ln["status"] == "ONBOARD") if _s["id"] == SCHOOL_STOP else 0
        if _pick or _board or _drop or _hand:
            _up.append({"Stop": _s["name"], "Planned": _s.get("eta") or "—", "To board": _pick + _board, "To get off": _drop + _hand})
    with st.expander(f"🗺️ Upcoming stops ({len(_up)})", expanded=bool(_up)):
        if _up:
            _cur_name = t("School") if cur_stop == SCHOOL_STOP else next((s["name"] for s in route_stops(route, _lines) if s["id"] == cur_stop), "")
            _df = pd.DataFrame(_up)
            st.dataframe(_df.style.apply(lambda r: ["background-color:#FFF4C2;font-weight:600" if r["Stop"] == _cur_name else "" for _ in r], axis=1),
                         hide_index=True, width="stretch")
        else:
            st.caption("No stops left — every child is accounted for.")
    left, right = st.columns(2)
    with left:
        ob = [ln for ln in _lines if ln["status"] in ("ONBOARD", "TRANSFER_PENDING")]
        off_here = [ln for ln in ob if (ln["purpose"] == "DROPOFF" and ln["stop_id"] == cur_stop) or
                    (ln["purpose"] == "PICKUP" and cur_stop == SCHOOL_STOP and ln["status"] == "ONBOARD")]
        st.subheader(f"🟢 {t('On board')} ({len(ob)})")
        if off_here:
            st.markdown(f"<span class='pill orange'>📍 {len(off_here)} {t('get off at the next stop') if is_driver else t('get off at this stop')}</span>",
                        unsafe_allow_html=True)
        lines_table(ob, ("name", "class", "purpose", "stop_name", "status", "entry_ts"), highlight={ln["id"] for ln in off_here})
    with right:
        ex = [ln for ln in _lines if ln["status"] == "EXPECTED"]
        here = [ln for ln in ex if (ln["purpose"] == "PICKUP" and ln["stop_id"] == cur_stop) or (ln["purpose"] == "DROPOFF" and cur_stop == SCHOOL_STOP)]
        st.subheader(f"⏳ {t('Expected')} ({len(ex)})")
        if here:
            st.markdown(f"<span class='pill orange'>📍 {len(here)} {t('expected at the next stop') if is_driver else t('expected at this stop')}</span>",
                        unsafe_allow_html=True)
        lines_table(sorted(ex, key=lambda ln: (ln not in here, ln["stop_seq"] or 0)), ("name", "class", "purpose", "stop_name"),
                    highlight={ln["id"] for ln in here})
        if ex and not is_driver:
            _cand = here or ex
            st.markdown("**" + t("Mark no-show") + "**" + (f" — {t('children expected at this stop')}" if here else ""))
            ns = st.pills(t("Mark no-show"), [ln["student_id"] for ln in _cand[:15]], key=f"{K}_ns", label_visibility="collapsed",
                          format_func=lambda i: next(ln["name"] for ln in _cand if ln["student_id"] == i))
            if st.button("🚫 " + t("Mark no-show"), disabled=ns is None, width="stretch", key=f"{K}_nsgo"):
                submit("NO_SHOW", sid=ns, method="CREW")
                st.rerun()


_lists_panel()

with st.expander(t("All children on this trip")):
    lines_table(lines, ("name", "code", "class", "batch", "grp", "purpose", "stop_name", "status", "entry_ts", "exit_ts", "handover",
                        "recipient_name", "received_by"))
    st.download_button(f"🖨 {t('Printable contingency roster')}", R.roster_pdf(lines, f"{bus['bus_no']} · {run['date']} · Trip {run['trip_no']}"),
                       file_name=f"{bus['bus_no']}_trip{run['trip_no']}_roster.pdf", mime="application/pdf")
with st.expander(t("Scan log")):
    ev = db.q("""SELECT substr(e.captured_ts,12,8) AS Captured, substr(e.received_ts,12,8) AS Received, e.kind AS Action, s.name AS Student,
                 e.result AS Result, e.reason AS Detail, e.method AS Method, e.operator AS Operator, e.connectivity AS Net
                 FROM events e LEFT JOIN students s ON s.id=e.student_id WHERE e.run_id=? ORDER BY e.id DESC""", (run["id"],))
    st.dataframe(ev, hide_index=True, width="stretch")
    if queue:
        st.caption(f"+ {len(queue)} action(s) on the device not yet synced")

if is_driver:
    st.stop()

# ------------------------------------------------------------------ close
st.divider()
st.subheader("⏹ " + t("Close trip"))
bl = [ln for ln in lines if ln["status"] in E.ACTIVE_LINE]
if bl:
    msg("error", f"{t('Cannot close: every child must be accounted for.')} {len(bl)} {t('Still to be accounted for').lower()}: "
        + ", ".join(f"{ln['name']} ({status_text(ln['status'])})" for ln in bl[:12]) + (" …" if len(bl) > 12 else ""))
    st.caption("Check out, mark no-show, return to school, or ask the office to decide pending transfers.")
crew_names = {run["supervisor_id"]: db.staff_name(run["supervisor_id"]), run["caretaker_id"]: db.staff_name(run["caretaker_id"])}
_sw_opts = list(dict.fromkeys(n for n in crew_names.values() if n))
sweeper = st.pills(t("Physical sweep done by"), _sw_opts, default=_sw_opts[-1] if _sw_opts else None, key=f"{K}_sweep") or (_sw_opts[-1] if _sw_opts else None)
swept = st.checkbox(t("I walked the full bus and checked every seat"), key=f"{K}_swept")
with st.expander("Final crew (change only if crew changed during the trip)"):
    f1, f2, f3 = st.columns(3)

    def fpick(col, label, r, default):
        opts = db.q("SELECT id, name FROM staff WHERE role=? ORDER BY bus_id=? DESC, name", (r, bus_id))
        ids = [o["id"] for o in opts]
        return col.selectbox(label, ids, index=ids.index(default) if default in ids else 0,
                             format_func=lambda i: next(o["name"] for o in opts if o["id"] == i), key=f"{K}_f{r}")
    fd = fpick(f1, t("Driver"), "driver", run["driver_id"])
    fs = fpick(f2, t("Supervisor"), "supervisor", run["supervisor_id"])
    fc = fpick(f3, t("Care-taker"), "caretaker", run["caretaker_id"])
if st.button("⏹ " + t("Close trip"), type="primary", disabled=bool(bl) or not swept, width="stretch"):
    g = gps_fix(cur_stop, gps_off)
    if offline:
        queue.append({"type": "close", "run_id": run["id"], "captured_ts": db.ts(), "sweep_by": sweeper, "final": [fd, fs, fc]})
        dstore.save_queue(dev_id, queue)
        st.session_state[f"{K}_last"] = ("warn", "Closed on the device — Pending Sync.")
    else:
        ok, m, _ = E.close_run(run["id"], sweeper, fd, fs, fc, g["lat"], g["lon"], user["username"], role)
        if ok:
            m += (f" Ended {db.now():%H:%M}, GPS {g['lat']:.5f}, {g['lon']:.5f}" if g["gps_ok"] else f" Ended {db.now():%H:%M}, GPS unavailable") + \
                 f" · sweep by {sweeper} · final crew {db.staff_name(fd)} / {db.staff_name(fs)} / {db.staff_name(fc)}."
        st.session_state[f"{K}_last"] = ("ok" if ok else "error", m)
        st.session_state.pop(f"{K}_swept", None)
    st.rerun()
