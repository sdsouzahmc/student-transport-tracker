"""Office (v2.0): dynamic trip planning — eligible roster -> safe stops -> ordered route, approval, in-trip revisions and audit."""
import json
from datetime import date

import pandas as pd
import pydeck as pdk
import streamlit as st

import db
import engine as E
import planner as P
from db import SCHOOL_STOP, TRIPS

user = st.session_state.user
role = user["role"]
can_approve = role in ("admin", "manager", "supervisor")
P.ensure_schema()

st.title("Trip planning")
st.caption("The planner builds each trip from the day's eligible roster (schedule, batch, group, purpose; absences and cancellations "
           "removed), groups students onto approved safe stops, and orders the stops by estimated road time while checking bus capacity "
           "at every stop and the school deadline. A plan becomes active only after a supervisor or the transport manager approves it.")

bl = db.buses()
_live = db.q1("SELECT bus_id, trip_no FROM trip_runs WHERE date=? AND status IN ('IN_PROGRESS','PENDING_SYNC') ORDER BY id DESC LIMIT 1",
              (db.today(),))     # default to a trip that is running now
_bids = [b["id"] for b in bl]
c = st.columns([1, 1, 2.2])
d = c[0].date_input("Date", value=date.fromisoformat(db.today()), key="pl_date").isoformat()
bus_id = c[1].selectbox("Bus", _bids, index=_bids.index(_live["bus_id"]) if _live and _live["bus_id"] in _bids else 0,
                        format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i), key="pl_bus")
cur_trip = _live["trip_no"] if _live else (E.trip_for_time() or 1)
trip = c[2].selectbox("Trip", list(TRIPS), index=list(TRIPS).index(cur_trip) if cur_trip in TRIPS else 0, key="pl_trip",
                      format_func=lambda n: f"Trip {n} · {TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]} · {TRIPS[n]['label']}")
bus = db.bus(bus_id)
run = P._run_for(d, bus_id, trip)

if st.session_state.get("pl_msg"):
    lvl, txt = st.session_state.pop("pl_msg")
    getattr(st, lvl)(txt)

# an approved change since the active plan? -> prepare a revised draft automatically
if run is None or run["status"] != "CLOSED":
    P.check_replan(d, bus_id, trip, user["username"], role)

act = P.active_plan(d, bus_id, trip)
dr = P.draft_plan(d, bus_id, trip)
allp = P.plans(d, bus_id, trip)

# ------------------------------------------------------------------ status + actions
with st.container(border=True):
    a, b = st.columns([2, 1.2])
    run_txt = f"Trip {run['status'].replace('_', ' ').lower()}" if run and run["status"] != "PLANNED" else "Trip not started"
    a.markdown(f"**{bus['bus_no']} · {d} · Trip {trip}** — {TRIPS[trip]['label']}  \n"
               f"<span style='color:#6B7390'>{run_txt} · window {TRIPS[trip]['window'][0]}–{TRIPS[trip]['window'][1]} · "
               f"capacity {bus['capacity']} · active plan: {('v' + str(act['version'])) if act else 'none'}"
               f"{' · draft v' + str(dr['version']) + ' waiting for approval' if dr else ''}</span>", unsafe_allow_html=True)
    closed = run and run["status"] == "CLOSED"
    with b:
        if closed:
            st.caption("Trip closed — plans are kept for the record.")
        else:
            with st.popover("Generate plan" if not act else "Recalculate remaining stops", icon=":material/route:", width="stretch"):
                why = st.text_input("Reason", value="Initial plan" if not act else "", key="pl_reason",
                                    placeholder="e.g. road closure on Al Waab Street")
                if st.button("Create draft", type="primary", key="pl_go"):
                    if not why.strip():
                        st.error("A reason is required.")
                    else:
                        P.create_draft(d, bus_id, trip, user["username"], role, why.strip())
                        st.session_state.pl_msg = ("success", "Draft created — review it below and approve to activate.")
                        st.rerun()

if dr:
    p_dr = json.loads(dr["plan_json"])
    with st.container(border=True):
        st.markdown(f"**Draft v{dr['version']}** · {dr['reason']} · created by {dr['created_by']} at {dr['created_ts'][11:16]}")
        if p_dr["violations"]:
            st.error("This plan cannot be activated: " + "; ".join(p_dr["violations"]))
        x1, x2, x3 = st.columns([1, 1, 2])
        if can_approve:
            if x1.button("Approve & activate", type="primary", icon=":material/verified:", disabled=bool(p_dr["violations"]), key="pl_ok"):
                ok, m_ = P.approve_plan(dr["id"], user["username"], role)
                st.session_state.pl_msg = ("success" if ok else "error", m_)
                st.rerun()
            if x2.button("Reject", icon=":material/close:", key="pl_no"):
                P.reject_plan(dr["id"], user["username"], role, "rejected by " + user["username"])
                st.session_state.pl_msg = ("info", f"Draft v{dr['version']} rejected — the active plan stays in force.")
                st.rerun()
        else:
            x1.caption("A supervisor or the transport manager approves routes.")
        if act:
            x3.caption("Activating a revision notifies the bus crew and the parents whose stop or time changed (more than 10 minutes).")

if not allp:
    p0 = P.plan_trip(bus_id, d, trip, run if run and run["status"] != "PLANNED" else None)
    st.info("No plan saved for this trip yet — below is a preview of what the planner would generate. Use **Generate plan** to save "
            "it as a draft for approval.")
    choices = {"preview": "Preview (not saved)"}
    plan_rows = {"preview": None}
else:
    choices = {r["id"]: f"v{r['version']} · {r['status'].title()} · {r['reason']}" for r in allp}
    plan_rows = {r["id"]: r for r in allp}
default = (dr or act or allp[0])["id"] if allp else "preview"
sel = st.segmented_control("Show plan", list(choices), default=default, format_func=choices.get, key=f"pl_sel_{d}_{bus_id}_{trip}") or default
row = plan_rows.get(sel)
plan = json.loads(row["plan_json"]) if row else p0
stops = P.stop_progress(plan, run if run and run["status"] != "PLANNED" else None)

# ------------------------------------------------------------------ KPIs + checks
k = st.columns(6)
k[0].metric("Students", plan["students"], help="Eligible: expected to board or on board (absences / cancellations excluded)")
k[1].metric("Stops", len([s for s in stops if s["stop_id"] != SCHOOL_STOP]))
k[2].metric("Peak on board", f"{plan['max_occupancy']} / {plan['capacity']}")
k[3].metric("School arrival", plan["school_arrival"] or "—", help=f"Deadline {plan['window'][1]}" if plan["school_arrival"] else "No school hand-over on this trip")
k[4].metric("Journey", f"{plan['total_min']:.0f} min")
k[5].metric("Excluded", len(plan["excluded"]), help="Absent, no-show or cancelled")

if plan["violations"]:
    st.error("**Rule violations** — " + " · ".join(plan["violations"]))
if plan["flags"]:
    with st.expander(f"⚠ {len(plan['flags'])} location issue(s) to fix before departure", expanded=True):
        st.dataframe(pd.DataFrame(plan["flags"]).drop(columns=["student_id"]), hide_index=True, width="stretch")
        st.page_link("page_locations.py", label="Fix in Student locations", icon=":material/home_pin:")
if plan["warnings"]:
    with st.expander(f"{len(plan['warnings'])} warning(s)"):
        for w in plan["warnings"]:
            st.markdown(f"- {w}")
if not (plan["violations"] or plan["flags"] or plan["warnings"]):
    st.success("All checks passed: every student has a valid location and safe stop, capacity is respected at every stop"
               + (" and the bus reaches school before the deadline." if plan["school_arrival"] else "."))

# ------------------------------------------------------------------ map + stop list
left, right = st.columns([1.1, 1])
with left:
    with st.container(border=True):
        st.markdown("**Route map**")
        show = st.pills("Layers", ["Route", "Registered home pins", "Scan GPS"], selection_mode="multi",
                        default=["Route", "Registered home pins", "Scan GPS"], key="pl_layers")
        layers = []
        slat, slon = db.school_pos()
        sdf = pd.DataFrame([{"lat": s["lat"], "lon": s["lon"], "seq": str(s["seq"]), "label": f"{s['seq']}. {s['name']} · ETA {s['eta']} · "
                             f"{len(s['board'])} on / {len(s['exit'])} off · {s.get('status', '')}",
                             "color": {"Done": [46, 158, 91], "Next": [224, 122, 31]}.get(s.get("status"), [61, 79, 214])} for s in stops])
        if "Route" in (show or []) and not sdf.empty:
            path = [[s["lon"], s["lat"]] for s in stops]
            layers.append(pdk.Layer("PathLayer", pd.DataFrame([{"path": path}]), get_path="path", get_color=[61, 79, 214, 170],
                                    width_min_pixels=4))
        if "Registered home pins" in (show or []):
            pins = []
            for s in stops:
                if s["stop_id"] == SCHOOL_STOP:
                    continue
                for st_ in s["board"] + s["exit"]:
                    if st_.get("loc") and st_["loc"][0] is not None:
                        pins.append({"lat": st_["loc"][0], "lon": st_["loc"][1], "label": f"Home pin · {st_['name']} · {st_.get('address') or ''}"})
                        pins[-1]["path"] = [[st_["loc"][1], st_["loc"][0]], [s["lon"], s["lat"]]]
            if pins:
                pdf = pd.DataFrame(pins)
                layers.append(pdk.Layer("PathLayer", pdf, get_path="path", get_color=[46, 158, 91, 110], width_min_pixels=1))
                layers.append(pdk.Layer("ScatterplotLayer", pdf, get_position="[lon, lat]", get_radius=12, radius_min_pixels=4,
                                        get_fill_color=[46, 158, 91, 200], pickable=True))
        if "Scan GPS" in (show or []) and run:
            ev = db.q("""SELECT e.lat, e.lon, e.kind, e.captured_ts, s.name FROM events e LEFT JOIN students s ON s.id=e.student_id
                         WHERE e.run_id=? AND e.lat IS NOT NULL AND e.result<>'REJECTED'""", (run["id"],))
            if ev:
                edf = pd.DataFrame([{"lat": e["lat"], "lon": e["lon"], "label": f"Scan GPS · {e['name'] or ''} · {e['kind']} {e['captured_ts'][11:16]}"}
                                    for e in ev])
                layers.append(pdk.Layer("ScatterplotLayer", edf, get_position="[lon, lat]", get_radius=10, radius_min_pixels=4,
                                        get_fill_color=[155, 81, 224, 210], pickable=True))
        if not sdf.empty:
            layers.append(pdk.Layer("ScatterplotLayer", sdf, get_position="[lon, lat]", get_radius=40, radius_min_pixels=11,
                                    get_fill_color="color", get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=2, pickable=True))
            layers.append(pdk.Layer("TextLayer", sdf, get_position="[lon, lat]", get_text="seq", get_size=12, get_color=[255, 255, 255],
                                    font_weight=700))
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame([{"lat": slat, "lon": slon, "label": "School"}]), get_position="[lon, lat]",
                                get_radius=60, radius_min_pixels=9, get_fill_color=[217, 48, 37], pickable=True))
        ctr = (sdf["lat"].mean(), sdf["lon"].mean()) if not sdf.empty else (slat, slon)
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=ctr[0], longitude=ctr[1], zoom=14),
                                 map_style="light", tooltip={"text": "{label}"}), height=430)
        st.caption("Numbered = planned stops (● done ● next ● pending) · ● registered home pin (only used to choose the safe stop) · "
                   "● GPS where a scan actually happened · ● School")
with right:
    with st.container(border=True):
        st.markdown("**Stop sequence**")
        tab = []
        for s in stops:
            tab.append({"#": s["seq"], "Stop": s["name"], "Boarding": len(s["board"]), "Exiting": len(s["exit"]), "Planned": s["eta"],
                        "Actual": s.get("actual") or "—", "On board after": s["occ_after"],
                        "Status": ("✅ " if s.get("status") == "Done" else "➡ " if s.get("status") == "Next" else "") + (s.get("status") or "")})
        st.dataframe(pd.DataFrame(tab), hide_index=True, width="stretch", height=min(420, 38 + 35 * len(tab)))
        for s in stops:
            if s["board"] or s["exit"]:
                with st.expander(f"{s['seq']}. {s['name']} — {len(s['board'])} boarding · {len(s['exit'])} exiting"):
                    if s["board"]:
                        st.markdown("**Boarding:** " + ", ".join(f"{x['name']}" for x in s["board"]))
                    if s["exit"]:
                        st.markdown("**Exiting:** " + ", ".join(f"{x['name']}" for x in s["exit"]))
        st.caption(plan["method"] + ". Planned start " + plan["start"] + ".")

# ------------------------------------------------------------------ per-student movements
st.subheader("Student movements")
lines = {ln["student_id"]: ln for ln in E.run_lines(run["id"])} if run and run["status"] != "PLANNED" else {}
mov = {}
for s in stops:
    for kind, lst in (("board", s["board"]), ("exit", s["exit"])):
        for x in lst:
            r = mov.setdefault(x["student_id"], {"x": x, "board": None, "exit": None, "dest": None})
            r[kind] = s
            if s["stop_id"] != SCHOOL_STOP:          # the student's own end of the journey (registered location -> safe stop)
                r["dest"] = x
st_rows = []
for sid_, r in mov.items():
    x, ln = r["x"], lines.get(sid_)
    dest_stop = r["exit"] if x["purpose"] == "DROPOFF" else r["board"]
    reg = r["dest"] or x
    scan_lat = (ln["exit_lat"] if x["purpose"] == "DROPOFF" else ln["entry_lat"]) if ln else None
    scan_lon = (ln["exit_lon"] if x["purpose"] == "DROPOFF" else ln["entry_lon"]) if ln else None
    dev = round(P.dist_m(scan_lat, scan_lon, dest_stop["lat"], dest_stop["lon"])) if scan_lat is not None and dest_stop else None
    st_rows.append({
        "Student": x["name"], "Code": x["code"], "Movement": "Drop-off" if x["purpose"] == "DROPOFF" else "Pickup",
        "Boards at": f"{r['board']['seq']}. {r['board']['name']} · {r['board']['eta']}" if r["board"] else "on board",
        "Exits at": f"{r['exit']['seq']}. {r['exit']['name']} · {r['exit']['eta']}" if r["exit"] else "—",
        "Default location (registered)": reg.get("address") or "—", "Walk to stop (m)": reg.get("walk_m"),
        "Status": ln["status"].replace("_", " ").title() if ln else "Expected",
        "Boarded": ((ln["entry_ts"] or "")[11:16] or "—") if ln else "—", "Exited": ((ln["exit_ts"] or "")[11:16] or "—") if ln else "—",
        "Scan GPS at own stop": f"{scan_lat:.5f}, {scan_lon:.5f}" if scan_lat is not None else "—",
        "Scan vs planned stop (m)": dev})
mv = pd.DataFrame(st_rows)
if not mv.empty:
    st.dataframe(mv, hide_index=True, width="stretch")
    st.caption("“Default location” is the student's approved registered address (used only to choose the safe stop); “Scan GPS” is where "
               "the crew actually scanned the badge at the student's own stop. Both are kept so any difference can be audited.")
if plan["excluded"]:
    st.caption("Excluded from this plan: " + ", ".join(f"{x['name']} ({x['status'].replace('_', ' ').lower()})" for x in plan["excluded"]))

# ------------------------------------------------------------------ revision history
st.subheader("Plan history")
if allp:
    hist = [{"Version": f"v{r['version']}", "Status": r["status"].title(), "Reason": r["reason"], "Created by": r["created_by"],
             "Created": r["created_ts"], "Approved / decided by": r["approved_by"] or "—", "Decided": r["approved_ts"] or "—",
             "Superseded": r["superseded_ts"] or "—", "Feasible": "Yes" if r["feasible"] else "No",
             "Stops": " → ".join(s["name"].replace("Route ", "R").replace(" Stop ", "S") for s in json.loads(r["plan_json"])["stops"])}
            for r in allp]
    st.dataframe(pd.DataFrame(hist), hide_index=True, width="stretch")
    st.caption("The original plan and every revision are kept with who created and approved it, when and why.")
else:
    st.caption("No saved plans for this trip.")
