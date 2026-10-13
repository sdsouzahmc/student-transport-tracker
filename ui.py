"""Small shared UI helpers."""
import random

import pandas as pd
import pydeck as pdk
import streamlit as st

import db
import engine as E_
from engine import STATUS_LABEL
from i18n import t


def msg(level, text):
    icon = {"ok": "✅", "warn": "⚠️", "error": "⛔"}[level]
    st.markdown(f'<div class="{level}">{icon} {text}</div>', unsafe_allow_html=True)


def status_text(s):
    return t(STATUS_LABEL.get(s, s or ""))


def gps_fix(stop_id, unavailable=False):
    """Simulated device GPS: position of the current stop with jitter and an accuracy value."""
    if unavailable:
        return {"lat": None, "lon": None, "accuracy": None, "gps_ok": False}
    stp = db.stop(stop_id)
    return {"lat": stp["lat"] + random.uniform(-.0003, .0003), "lon": stp["lon"] + random.uniform(-.0003, .0003),
            "accuracy": round(random.uniform(4, 18), 1), "gps_ok": True}


def bus_positions(date=None):
    """Last known position per bus = latest accepted event with coordinates on its most recent run (else trip start)."""
    rows = []
    for b in db.buses():
        run = db.q1("SELECT * FROM trip_runs WHERE bus_id=? AND start_ts IS NOT NULL" + (" AND date=?" if date else "") +
                    " ORDER BY start_ts DESC LIMIT 1", (b["id"], date) if date else (b["id"],))
        if not run:
            continue
        ev = db.q1("""SELECT lat, lon, captured_ts, received_ts FROM events WHERE run_id=? AND lat IS NOT NULL AND result<>'REJECTED'
                      ORDER BY captured_ts DESC LIMIT 1""", (run["id"],))
        lat, lon, when = (ev["lat"], ev["lon"], ev["captured_ts"]) if ev else (run["start_lat"], run["start_lon"], run["start_ts"])
        if run["end_ts"] and run["end_lat"]:
            lat, lon, when = run["end_lat"], run["end_lon"], run["end_ts"]
        onboard = db.q1("SELECT COUNT(*) n FROM manifest WHERE run_id=? AND status IN ('ONBOARD','TRANSFER_PENDING')", (run["id"],))["n"]
        rows.append({"bus": b["bus_no"], "bus_id": b["id"], "run_id": run["id"], "trip": run["trip_no"], "status": run["status"],
                     "lat": lat, "lon": lon, "when": when, "onboard": onboard})
    return rows


def bus_map(points, stops=None, height=420, zoom=11.5):
    if not points and not stops:
        return
    df = pd.DataFrame(points)
    layers = []
    if stops:
        sdf = pd.DataFrame(stops)
        layers.append(pdk.Layer("ScatterplotLayer", sdf, get_position="[lon, lat]", get_radius=60, radius_min_pixels=3,
                                get_fill_color=[120, 130, 150, 160], pickable=True))
    if not df.empty:
        df["color"] = df["status"].map(lambda s: [217, 48, 37] if s == "ATTN" else ([30, 142, 62] if s in ("IN_PROGRESS", "PENDING_SYNC")
                                                                                   else [28, 84, 144]))
        df["label"] = df.apply(lambda r: f"{r['bus']} · {r.get('onboard', 0)} on board", axis=1)
        layers.append(pdk.Layer("ScatterplotLayer", df, get_position="[lon, lat]", get_radius=120, radius_min_pixels=8,
                                get_fill_color="color", pickable=True))
        layers.append(pdk.Layer("TextLayer", df, get_position="[lon, lat]", get_text="bus", get_size=13, get_pixel_offset=[0, -18],
                                get_color=[20, 20, 20]))
    lat, lon = db.school_pos()
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom),
                             map_style="light", tooltip={"text": "{label}{name}"}), height=height)


def lines_table(lines, cols=("name", "class", "batch", "grp", "purpose", "stop_name", "status", "entry_ts", "exit_ts", "handover"),
                highlight=None):
    """highlight: set of manifest line ids to mark (children to act on at the selected stop) — shown first, tinted and with 📍."""
    if not lines:
        st.caption("—")
        return
    names = {"name": t("Student"), "code": "ID", "class": t("Class"), "batch": t("Batch"), "grp": t("Group"), "purpose": t("Purpose"), "stop_name": t("Stop"),
             "status": t("Status"), "entry_ts": t("Boarded"), "exit_ts": t("Got off"), "handover": t("Handover"),
             "recipient_name": t("Handed to"), "received_by": t("Received by"), "note": t("Note")}
    hl = set(highlight or ())
    if hl:
        lines = sorted(lines, key=lambda ln: ln.get("id") not in hl)
    df = pd.DataFrame(lines)
    mark = [ln.get("id") in hl for ln in lines]
    df = df[[c for c in cols if c in df]]
    if "status" in df:
        df["status"] = df["status"].map(status_text)
    for c in ("entry_ts", "exit_ts"):
        if c in df:
            df[c] = df[c].fillna("").str[11:16]
    if "purpose" in df:
        df["purpose"] = df["purpose"].map(lambda v: t((v or "").title()))
    if "stop_name" in df:
        df["stop_name"] = df["stop_name"].fillna(t("School"))
    for c in ("recipient_name", "received_by", "handover", "note"):
        if c in df:
            df[c] = df[c].fillna("—")
    df = df.rename(columns=names)
    if hl and any(mark):
        df.insert(0, " ", ["📍" if m else "" for m in mark])
        sty = df.style.apply(lambda row: ["background-color:#FFF4C2;font-weight:600" if mark[row.name] else "" for _ in row], axis=1)
        st.dataframe(sty, hide_index=True, width="stretch")
    else:
        st.dataframe(df, hide_index=True, width="stretch")


ACTIVE_ST = ("EXPECTED", "ONBOARD", "TRANSFER_PENDING")


def _run_lines(run_id):
    return db.q("""SELECT m.*, s.code, s.name, s.class_name || s.section AS class, st.name AS stop_name, st.seq AS stop_seq
                   FROM manifest m JOIN students s ON s.id=m.student_id LEFT JOIN stops st ON st.id=m.stop_id
                   WHERE m.run_id=? ORDER BY st.seq, s.name""", (run_id,))


def trip_info(r):
    """Summary of one trip run for cards and dialogs."""
    b = db.bus(r["bus_id"])
    lines = _run_lines(r["id"])
    total = len(lines)
    done = sum(1 for ln in lines if ln["status"] not in ACTIVE_ST)
    onb = sum(1 for ln in lines if ln["status"] in ("ONBOARD", "TRANSFER_PENDING"))
    exp_ = sum(1 for ln in lines if ln["status"] == "EXPECTED")
    purposes = {ln["purpose"] for ln in lines}
    kind = "Pickup" if purposes == {"PICKUP"} else ("Drop-off" if purposes == {"DROPOFF"} else "Drop-off + pickup")
    movements = sorted({f"Batch {ln['batch']} {ln['grp']} {'pickup' if ln['purpose'] == 'PICKUP' else 'drop-off'}" for ln in lines})
    area = (b["route_name"] or "").replace("Route ", "R")
    ends = (area or "Route", "School") if kind == "Pickup" else ("School", area or "Route")
    closed = r["status"] == "CLOSED"
    pend = sorted([ln for ln in lines if (ln["status"] == "EXPECTED" and ln["purpose"] == "PICKUP") or
                   (ln["status"] == "ONBOARD" and ln["purpose"] == "DROPOFF")], key=lambda ln: ln["stop_seq"] or 0)
    if closed:
        nxt = f"Closed {(r['end_ts'] or '')[11:16]}"
    elif any(ln["status"] == "EXPECTED" and ln["purpose"] == "DROPOFF" for ln in lines):
        nxt = "School (boarding)"
    elif pend:
        nxt = pend[0]["stop_name"] or "School"
    elif onb:
        nxt = "School gate"
    else:
        nxt = "Ready to close"
    return dict(bus=b, lines=lines, total=total, done=done, onb=onb, exp=exp_, kind=kind, movements=movements, ends=ends,
                closed=closed, offline=r["status"] == "PENDING_SYNC", nxt=nxt, frac=(done / total) if total else (1.0 if closed else 0.0))


def _card_html(r, k):
    b = k["bus"]
    sos = db.q1("SELECT ts FROM incidents WHERE bus_id=? AND kind='SOS' AND status<>'RESOLVED' ORDER BY id DESC LIMIT 1", (r["bus_id"],))
    if sos:
        pill = f"<span class='pill red'>🆘 SOS {sos['ts'][11:16]}</span>"
    elif k["closed"]:
        pill = f"<span class='pill green'>Closed {(r['end_ts'] or '')[11:16]}</span>"
    elif k["offline"]:
        pill = "<span class='pill orange'>Offline</span>"
    else:
        pill = f"<span class='pill'>{k['kind']}</span>"
    onboard_v = (f"{k['done']} / {k['total']} done" if k["closed"] else f"{k['onb']} / {k['onb'] + k['exp']}")
    onboard_l = "Accounted for" if k["closed"] else "Onboard"
    return f"""<div class='bc flat{' closed' if k['closed'] else ''}'><div class='top'><span class='no'>{b['bus_no']}</span><span class='area'>Trip {r['trip_no']}</span>
<span style='margin-left:auto'>{pill}</span></div>
<div class='prog'><div class='f' style='width:{k['frac'] * 100:.0f}%'></div><div class='k' style='left:calc({k['frac'] * 100:.0f}% - 8px)'></div></div>
<div class='ends'><span>{k['ends'][0]}</span><span>{k['ends'][1]}</span></div>
<div class='grid'><div><div class='l'>Movement</div><div class='v'>{'<br>'.join(k['movements'][:2]) or '—'}</div></div>
<div><div class='l'>{onboard_l}</div><div class='v'>{onboard_v}</div><div class='l'>{b['capacity']} seats</div></div>
<div><div class='l'>Driver</div><div class='v'>{staff_name(r['final_driver_id'] or r['driver_id'])}</div></div>
<div><div class='l'>Attendant</div><div class='v'>{staff_name(r['final_caretaker_id'] or r['caretaker_id'])}</div></div>
<div><div class='l'>{'Status' if k['closed'] else 'Next'}</div><div class='v'>{k['nxt']}</div></div>
<div><div class='l'>Progress</div><div class='v'>{k['done']} / {k['total']} done</div></div></div></div>"""


def bus_cards(runs, key="bc", per_row=3):
    """Trip cards in a grid, each with Locate on map and Students buttons (open a dialog)."""
    for i in range(0, len(runs), per_row):
        cols = st.columns(per_row)
        for col, r in zip(cols, runs[i:i + per_row]):
            k = trip_info(r)
            with col, st.container(border=True):
                st.markdown(_card_html(r, k), unsafe_allow_html=True)
                b1, b2 = st.columns(2)
                if b1.button("Locate on map", key=f"{key}_loc_{r['id']}", width="stretch", icon=":material/location_on:"):
                    locate_dialog(r["id"])
                if b2.button("Students", key=f"{key}_stu_{r['id']}", width="stretch", icon=":material/group:"):
                    students_dialog(r["id"])
                if True:   # also on closed trips: the crew still sees messages between trips
                    n_open = len([m_ for m_ in E_.bus_messages(r["bus_id"], only_open=True)])
                    if st.button("Message bus" + (f" ({n_open} unread)" if n_open else ""), key=f"{key}_msg_{r['id']}", width="stretch",
                                 icon=":material/campaign:"):
                        message_dialog(r["id"])


def run_position(r):
    """Last known position of a trip: end GPS if closed, else latest accepted scan with GPS, else start GPS."""
    if r["status"] == "CLOSED" and r["end_lat"] is not None:
        return r["end_lat"], r["end_lon"], r["end_ts"], "trip end"
    ev = db.q1("""SELECT lat, lon, captured_ts FROM events WHERE run_id=? AND lat IS NOT NULL AND result<>'REJECTED'
                  ORDER BY captured_ts DESC LIMIT 1""", (r["id"],))
    if ev:
        return ev["lat"], ev["lon"], ev["captured_ts"], "last scan"
    return r["start_lat"], r["start_lon"], r["start_ts"], "trip start"


@st.dialog("Locate on map", width="large")
def locate_dialog(run_id):
    r = db.q1("SELECT * FROM trip_runs WHERE id=?", (run_id,))
    k = trip_info(r)
    b = k["bus"]
    lat, lon, when, src = run_position(r)
    st.markdown(f"### {b['bus_no']} · Trip {r['trip_no']}")
    st.caption(f"{k['kind']} · {'Closed ' + (r['end_ts'] or '')[11:16] if k['closed'] else ('Offline' if k['offline'] else 'Moving')} · "
               f"position from {src} {(when or '')[11:16]} · next: {k['nxt']}")
    stops = [{"lat": s_["lat"], "lon": s_["lon"], "name": s_["name"]} for s_ in db.stops_of_bus(b["id"]) if s_["lat"] is not None]
    slat, slon = db.school_pos()
    stops.append({"lat": slat, "lon": slon, "name": "School"})
    layers = [pdk.Layer("PathLayer", pd.DataFrame([{"path": [[s_["lon"], s_["lat"]] for s_ in stops]}]), get_path="path",
                        get_color=[61, 79, 214, 120], width_min_pixels=3),
              pdk.Layer("ScatterplotLayer", pd.DataFrame(stops), get_position="[lon, lat]", get_radius=50, radius_min_pixels=5,
                        get_fill_color=[120, 130, 150, 200], pickable=True),
              pdk.Layer("TextLayer", pd.DataFrame(stops), get_position="[lon, lat]", get_text="name", get_size=12,
                        get_pixel_offset=[0, 16], get_color=[70, 80, 110])]
    if lat is not None:
        bdf = pd.DataFrame([{"lat": lat, "lon": lon, "name": f"{b['bus_no']} · {k['onb']} on board"}])
        layers += [pdk.Layer("ScatterplotLayer", bdf, get_position="[lon, lat]", get_radius=90, radius_min_pixels=10,
                             get_fill_color=[30, 142, 62] if not k["closed"] else [28, 84, 144], pickable=True),
                   pdk.Layer("TextLayer", bdf, get_position="[lon, lat]", get_text="name", get_size=14, get_pixel_offset=[0, -20],
                             get_color=[20, 20, 20])]
    else:
        st.info("No GPS position recorded for this trip yet.")
    clat, clon = (lat, lon) if lat is not None else (slat, slon)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=clat, longitude=clon, zoom=13.5),
                             map_style="light", tooltip={"text": "{name}"}), height=430)
    if lat is not None:
        st.caption(f"GPS {lat:.5f}, {lon:.5f}. In production the position comes live from the bus GPS unit.")


@st.dialog("Students", width="large")
def students_dialog(run_id):
    r = db.q1("SELECT * FROM trip_runs WHERE id=?", (run_id,))
    k = trip_info(r)
    b = k["bus"]
    status = f"Closed {(r['end_ts'] or '')[11:16]}" if k["closed"] else ("Offline" if k["offline"] else "Moving")
    st.markdown(f"### {b['bus_no']} · Trip {r['trip_no']}")
    st.markdown(f"<span class='pill {'green' if k['closed'] else ('orange' if k['offline'] else '')}'>{status}</span> "
                + " ".join(f"<span class='pill grey'>{m}</span>" for m in k["movements"]), unsafe_allow_html=True)
    c = st.columns(4)
    c[0].metric("On board", k["onb"])
    c[1].metric("Still to pick up", k["exp"])
    c[2].metric("Accounted for", f"{k['done']} / {k['total']}")
    c[3].metric("Next" if not k["closed"] else "Status", k["nxt"])
    st.caption(f"Driver {staff_name(r['final_driver_id'] or r['driver_id'])} · Supervisor {staff_name(r['final_supervisor_id'] or r['supervisor_id'])} · "
               f"Attendant {staff_name(r['final_caretaker_id'] or r['caretaker_id'])} · started {(r['start_ts'] or '')[11:16]}"
               + (f" · closed {(r['end_ts'] or '')[11:16]} · sweep by {r['sweep_by'] or '—'}" if k["closed"] else ""))
    groups = [("On board", [ln for ln in k["lines"] if ln["status"] in ("ONBOARD", "TRANSFER_PENDING")],
               ("name", "code", "class", "purpose", "stop_name", "status", "entry_ts")),
              ("Still to pick up", [ln for ln in k["lines"] if ln["status"] == "EXPECTED"], ("name", "code", "class", "purpose", "stop_name")),
              ("Completed", [ln for ln in k["lines"] if ln["status"] in ("COMPLETED", "RETURNED", "TRANSFERRED_OUT")],
               ("name", "code", "class", "purpose", "stop_name", "entry_ts", "exit_ts", "recipient_name", "received_by")),
              ("Absent / no-show", [ln for ln in k["lines"] if ln["status"] in ("ABSENT_DECLARED", "NO_SHOW", "CANCELLED")],
               ("name", "code", "class", "purpose", "stop_name", "status"))]
    if k["closed"]:
        groups = groups[2:] + groups[:2]          # closed trip: show who was handed over first
    tabs = st.tabs([f"{g} ({len(ls)})" for g, ls, _ in groups])
    for tab, (g, ls, cols) in zip(tabs, groups):
        with tab:
            lines_table(ls, cols)


def staff_name(i):
    return db.staff_name(i) or "—"


def alert_sound(say=None, key="snd"):
    """Short beep (and optional spoken text) in the browser — used for new office messages and the driver's next stop."""
    import json
    say_js = json.dumps(say or "").replace("</", "<\\/")
    js = f"""<html><body style='margin:0'><script>
try {{
  const C = window.parent.AudioContext || window.parent.webkitAudioContext || AudioContext;
  const a = new C(); const o = a.createOscillator(); const g = a.createGain();
  o.type = 'sine'; o.frequency.value = 880; o.connect(g); g.connect(a.destination);
  g.gain.setValueAtTime(0.25, a.currentTime); o.start(); o.stop(a.currentTime + 0.35);
}} catch (e) {{}}
try {{
  const txt = {say_js};
  if (txt && window.parent.speechSynthesis) {{ const u = new SpeechSynthesisUtterance(txt); u.rate = 0.95; window.parent.speechSynthesis.speak(u); }}
}} catch (e) {{}}
</script></body></html>"""
    st.iframe(js, height=1)


QUICK_MSGS = ["Please call the transport office now", "Traffic on your route — expect a delay, parents informed",
              "Wait at the stop: a parent is on the way", "Return to school with all children on board",
              "Change of route: follow the office instructions by phone"]


@st.dialog("Message bus", width="large")
def message_dialog(run_id):
    E = E_
    r = db.q1("SELECT * FROM trip_runs WHERE id=?", (run_id,))
    b = db.bus(r["bus_id"])
    st.markdown(f"### {b['bus_no']} · Trip {r['trip_no']}")
    st.caption("The message appears on the bus crew and driver screens with a sound alert, until a crew member taps Acknowledge"
               + (" — this trip is closed, so it reaches the crew wherever they are in the app." if r["status"] == "CLOSED" else "."))
    quick = st.pills("Quick messages", QUICK_MSGS, key=f"qm_{run_id}")
    text = st.text_area("Message", value=quick or "", key=f"mt_{run_id}_{quick or ''}", max_chars=300)
    if st.button("Send to bus", type="primary", icon=":material/send:", disabled=not text.strip(), key=f"ms_{run_id}"):
        u_ = st.session_state.user
        E.send_bus_message(b["id"], run_id, text.strip(), u_["display_name"], u_["role"])
        st.success(f"Sent to {b['bus_no']}.")
    hist = E.bus_messages(b["id"], limit=10)
    if hist:
        st.markdown("**Recent messages**")
        st.dataframe(pd.DataFrame([{"Sent": h["ts"][11:16], "From": h["sender"], "Message": h["text"],
                                    "Acknowledged": f"{h['ack_ts'][11:16]} by {h['ack_by']}" if h["ack_ts"] else "⏳ not yet"} for h in hist]),
                     hide_index=True, width="stretch")
