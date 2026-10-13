"""Executive dashboard (TV display): fleet status, student counts and open alerts at a glance — live data, refreshes itself."""
from collections import Counter

import pydeck as pdk
import pandas as pd
import streamlit as st

import db
import engine as E
from db import TRIPS
from ui import bus_positions

ICON = {
    "people": "<svg viewBox='0 0 24 24' fill='#3D4FD6'><path d='M16 11a3 3 0 1 0-3-3 3 3 0 0 0 3 3Zm-8 0a3 3 0 1 0-3-3 3 3 0 0 0 3 3Zm0 2c-2.3 0-7 1.2-7 3.5V19h14v-2.5C15 14.2 10.3 13 8 13Zm8 0c-.3 0-.6 0-1 .1a4.2 4.2 0 0 1 2 3.4V19h6v-2.5c0-2.3-4.7-3.5-7-3.5Z'/></svg>",
    "bus": "<svg viewBox='0 0 24 24' fill='none' stroke='#3D4FD6' stroke-width='2'><rect x='4' y='3' width='16' height='14' rx='3'/><path d='M4 11h16M8 17v3M16 17v3'/><circle cx='8' cy='14' r='.5'/><circle cx='16' cy='14' r='.5'/></svg>",
    "pin": "<svg viewBox='0 0 24 24' fill='#3D4FD6'><path d='M12 2a7 7 0 0 0-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 0 0-7-7Zm0 9.5A2.5 2.5 0 1 1 14.5 9 2.5 2.5 0 0 1 12 11.5Z'/></svg>",
    "check": "<svg viewBox='0 0 24 24'><circle cx='12' cy='12' r='10' fill='#2E9E5B'/><path d='m7.5 12.5 3 3 6-6' stroke='#fff' stroke-width='2.4' fill='none'/></svg>",
    "warn": "<svg viewBox='0 0 24 24'><path d='M12 3 1.8 20.5h20.4Z' fill='#E07A1F'/><path d='M12 9v5M12 16.5v.5' stroke='#fff' stroke-width='2.2'/></svg>",
    "chart": "<svg viewBox='0 0 24 24' fill='#3D4FD6'><rect x='4' y='10' width='4' height='10' rx='1'/><rect x='10' y='5' width='4' height='15' rx='1'/><rect x='16' y='13' width='4' height='7' rx='1'/></svg>",
    "shield": "<svg viewBox='0 0 24 24' fill='#3D4FD6'><path d='M12 2 4 5v6c0 5 3.4 9.6 8 11 4.6-1.4 8-6 8-11V5Z'/></svg>",
    "cal": "<svg viewBox='0 0 24 24' fill='none' stroke='#3D4FD6' stroke-width='2'><rect x='3' y='5' width='18' height='16' rx='2'/><path d='M3 10h18M8 3v4M16 3v4'/></svg>",
    "mail": "<svg viewBox='0 0 24 24' fill='none' stroke='#3D4FD6' stroke-width='2' width='26' height='26'><rect x='3' y='5' width='18' height='14' rx='2'/><path d='m3 7 9 6 9-6'/></svg>",
    "clock": "<svg viewBox='0 0 24 24' fill='none' stroke='#1A2036' stroke-width='2' width='26' height='26'><circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/></svg>",
    "ok": "<svg viewBox='0 0 24 24' width='26' height='26'><circle cx='12' cy='12' r='10' fill='#2E9E5B'/><path d='m7.5 12.5 3 3 6-6' stroke='#fff' stroke-width='2.4' fill='none'/></svg>",
}
MOVE = lambda b, g, p: f"Batch {b} {g} {'pickup' if p == 'PICKUP' else 'drop-off'}"

school = db.setting("school_name", db.SCHOOL_NAME)
h1, h2 = st.columns([3, 2], vertical_alignment="center")
h1.markdown(f"<div style='font-size:1.9rem;font-weight:800;line-height:1.1'>Student Transport Executive Dashboard</div>"
            f"<div class='muted' style='font-size:.95rem'>{school}</div>", unsafe_allow_html=True)
with h2:
    c1, c2 = st.columns([3, 2], vertical_alignment="center")
    auto = c2.toggle("Live", value=True, help="Refresh every 15 seconds (for a TV screen)")
    c1.markdown(f"<div style='text-align:right'>"
                f"<span style='color:#3A4160;white-space:nowrap'>{db.now():%d %b %Y} &nbsp;|&nbsp; {db.now():%I:%M %p} &nbsp;|&nbsp; Qatar</span></div>",
                unsafe_allow_html=True)


_bl = db.buses()
_bus_no = {b_["id"]: b_["bus_no"] for b_ in _bl}
st.pills("Bus", ["ALL"] + [b_["id"] for b_ in _bl], default="ALL", key="dash_bus", label_visibility="collapsed",
         format_func=lambda i: "All buses" if i == "ALL" else _bus_no.get(i, i))


@st.fragment(run_every=15 if auto else None)
def board():
    D = db.today()
    BUS = st.session_state.get("dash_bus") or "ALL"
    one = BUS != "ALL"

    def bw(col):
        """SQL filter on the selected bus (empty when All buses)."""
        return (f" AND {col}=?", [BUS]) if one else ("", [])
    f_s, p_s = bw("bus_id")
    students = db.q1("SELECT COUNT(*) n FROM students WHERE active=1" + f_s, p_s)["n"]
    nbus = 1 if one else db.q1("SELECT COUNT(*) n FROM buses")["n"]
    op_bus = db.q1("SELECT COUNT(DISTINCT bus_id) n FROM trip_runs WHERE date=?" + f_s, [D] + p_s)["n"]
    active = db.q("SELECT * FROM trip_runs WHERE status IN ('IN_PROGRESS','PENDING_SYNC') AND date=?" + f_s, [D] + p_s)
    open_ids = [r["id"] for r in active] or [-1]
    ph = ",".join("?" * len(open_ids))
    onboard_lines = db.q(f"SELECT batch, grp, purpose FROM manifest WHERE run_id IN ({ph}) AND status IN ('ONBOARD','TRANSFER_PENDING')", open_ids)
    f_r, p_r = bw("r.bus_id")
    handovers = db.q1("""SELECT COUNT(*) n FROM manifest m JOIN trip_runs r ON r.id=m.run_id
                         WHERE r.date=? AND m.handover IN ('SCHOOL_RECEIPT','HOME_RELEASE')""" + f_r, [D] + p_r)["n"]
    f_i, p_i = bw("i.bus_id")
    alerts = db.q("""SELECT i.*, b.bus_no FROM incidents i LEFT JOIN buses b ON b.id=i.bus_id WHERE i.status<>'RESOLVED'""" + f_i +
                  """ ORDER BY CASE i.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, i.id DESC""", p_i)

    def kpi(icon, label, value, sub="", cls=""):
        return (f"<div class='kpi'><div class='lab'>{ICON[icon]}{label}</div><div class='val {cls}'>{value:,}</div>"
                f"<div class='sub'>{sub}&nbsp;</div></div>")
    st.markdown("<div class='kpis'>" + kpi("people", "Registered Students" if not one else f"Students on {_bus_no.get(BUS)}", students) + kpi("bus", "Operating Buses", op_bus, f"of {nbus} today")
                + kpi("pin", "Active Trips", len(active)) + kpi("people", "Students Onboard", len(onboard_lines))
                + kpi("check", "Completed Handovers", handovers, "today") + kpi("warn", "Open Alerts", len(alerts), "", "orange" if alerts else "")
                + "</div>", unsafe_allow_html=True)

    left, right = st.columns([1.55, 1])
    with left, st.container(border=True):
        st.markdown(f"<div class='sect'>{ICON['pin']}Bus Location Overview <span class='muted' style='margin-left:auto;font-weight:400'>"
                    "Vehicle locations · last scan position</span></div>", unsafe_allow_html=True)
        pos = [x_ for x_ in bus_positions() if not one or x_["bus_id"] == BUS]
        slat, slon = db.school_pos()
        layers = []
        if pos:
            pdf = pd.DataFrame(pos)
            pdf["color"] = pdf["status"].map(lambda s: [61, 79, 214] if s in ("IN_PROGRESS", "PENDING_SYNC") else [120, 128, 150])
            pdf["label"] = pdf.apply(lambda r: f"{r['bus']} · Trip {r['trip']} · {r['onboard']} on board", axis=1)
            layers += [pdk.Layer("ScatterplotLayer", pdf, get_position="[lon, lat]", get_radius=90, radius_min_pixels=9,
                                 get_fill_color="color", get_line_color=[255, 255, 255], line_width_min_pixels=2, stroked=True, pickable=True),
                       pdk.Layer("TextLayer", pdf, get_position="[lon, lat]", get_text="bus", get_size=13, get_pixel_offset=[34, 0],
                                 get_color=[26, 32, 54], font_weight=700)]
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame([{"lat": slat, "lon": slon, "label": school}]), get_position="[lon, lat]",
                                get_radius=110, radius_min_pixels=10, get_fill_color=[217, 48, 37], pickable=True))
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=slat, longitude=slon, zoom=12.3),
                                 map_style="light", tooltip={"text": "{label}"}), height=330)
        st.caption("● Moving (trip open) · ● Idle / closed · ● School")
    with right:
        with st.container(border=True):
            st.markdown(f"<div class='sect'>{ICON['chart']}Students Onboard by Movement</div>", unsafe_allow_html=True)
            cnt = Counter(MOVE(l["batch"], l["grp"], l["purpose"]) for l in onboard_lines)
            if not cnt:
                st.caption("No students on board right now.")
            else:
                top = max(cnt.values())
                shades = ["#3D4FD6", "#8EA2F5", "#B9C6F8", "#D4DCFA"]
                html = "".join(f"<div class='bar'><div class='name'>{k}</div><div class='track'><div class='fill' style='width:{100 * v / top:.0f}%;"
                               f"background:{shades[min(i, 3)]}'></div></div><div class='num'>{v}</div></div>"
                               for i, (k, v) in enumerate(cnt.most_common()))
                st.markdown(html, unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown(f"<div class='sect'>{ICON['shield']}Safety and Communication</div>", unsafe_allow_html=True)
            closed = db.q1("SELECT COUNT(*) n, SUM(sweep_by IS NOT NULL AND sweep_by<>'') s FROM trip_runs WHERE date=? AND status='CLOSED'" + f_s,
                           [D] + p_s)
            _nj = " LEFT JOIN trip_runs r ON r.id=n.run_id"
            nt = db.q1("SELECT COUNT(*) n, SUM(n.status='SENT') s FROM notifications n" + _nj + " WHERE substr(n.ts,1,10)=?" + f_r, [D] + p_r)
            if not nt["n"]:
                nt = db.q1("SELECT COUNT(*) n, SUM(n.status='SENT') s FROM notifications n" + _nj + " WHERE 1=1" + f_r, p_r)
            pct = 100 * (nt["s"] or 0) / nt["n"] if nt["n"] else 100
            last = db.q1("SELECT MAX(e.captured_ts) t FROM events e LEFT JOIN trip_runs r ON r.id=e.run_id WHERE e.result<>'REJECTED'" + f_r,
                         p_r)["t"]
            last_txt = pd.Timestamp(last).strftime("%I:%M %p") if last else "—"
            st.markdown(f"<div class='safety'><div><div class='lab'>Bus sweeps today</div><div class='v'>{ICON['ok']}"
                        f"<span style='color:#2E9E5B'>{closed['s'] or 0} / {closed['n'] or 0}</span></div></div>"
                        f"<div><div class='lab'>Parent notices delivered</div><div class='v'>{ICON['mail']}"
                        f"<span style='color:#3D4FD6'>{pct:.1f}%</span></div></div>"
                        f"<div><div class='lab'>Last student update</div><div class='v' style='white-space:nowrap;font-size:1.25rem'>{ICON['clock']}{last_txt}</div></div></div>",
                        unsafe_allow_html=True)

    left, right = st.columns([1.55, 1])
    with left, st.container(border=True):
        st.markdown(f"<div class='sect'>{ICON['cal']}Daily Trip Schedule</div>", unsafe_allow_html=True)
        rows = ""
        for no, tr in TRIPS.items():
            runs = db.q("SELECT status, start_ts, end_ts FROM trip_runs WHERE date=? AND trip_no=?" + f_s, [D, no] + p_s)
            if any(r["status"] in ("IN_PROGRESS", "PENDING_SYNC") for r in runs):
                pill = f"<span class='pill blue'>● Active</span>"
            elif runs:
                pill = "<span class='pill green'>Completed</span>"
            else:
                pill = "<span class='pill grey'>Scheduled</span>"
            if one and runs:
                r0 = runs[0]
                buses = (f"started {(r0['start_ts'] or '')[11:16]}" + (f" · closed {(r0['end_ts'] or '')[11:16]}" if r0["end_ts"] else ""))
            else:
                buses = f"{len(runs)} bus{'es' if len(runs) != 1 else ''}" if runs else ""
            rows += (f"<tr><td style='padding:9px 8px;white-space:nowrap'>Trip {no}</td><td>{tr['label']}</td>"
                     f"<td style='white-space:nowrap'>{tr['window'][0]}–{tr['window'][1]}</td><td>{pill} <span class='muted'>{buses}</span></td></tr>")
        st.markdown("<table style='width:100%;border-collapse:collapse;font-size:.9rem'><thead><tr style='background:#F4F6FA;color:#3A4160;text-align:left'>"
                    "<th style='padding:8px'>Trip</th><th>Student movement</th><th>Window</th><th>Status</th></tr></thead><tbody>"
                    + rows.replace("<tr>", "<tr style='border-top:1px solid #E3E7F0'>") + "</tbody></table>", unsafe_allow_html=True)
    with right, st.container(border=True):
        st.markdown(f"<div class='sect'>{ICON['warn']}Attention Required</div>", unsafe_allow_html=True)
        if not alerts:
            st.success("Nothing needs attention.")
        for a in alerts[:4]:
            st.markdown(f"<div class='att'><span><span class='dot'></span><b>{a['bus_no'] or '—'}</b> · {a['kind'].replace('_', ' ').title()}</span>"
                        f"<span class='muted'>{(a['ts'] or '')[11:16]}</span></div>", unsafe_allow_html=True)
        a1, a2 = st.columns([1, 1], vertical_alignment="center")
        a1.markdown(f"<span style='color:#E07A1F;font-weight:700'>{len(alerts)} open item{'s' if len(alerts) != 1 else ''}</span>", unsafe_allow_html=True)
        a2.page_link("page_exceptions.py", label="View incidents", icon=":material/arrow_forward:")
    st.caption("Live data from the crew app · student locations recorded at scan events · refreshes every 15 s")


board()
