"""Office (v2.0): student default pickup / drop-off locations — registration, school approval, history and pre-departure checks."""
import pandas as pd
import pydeck as pdk
import streamlit as st

import db
import planner as P

user = st.session_state.user
role = user["role"]
can_approve = role in ("admin", "manager")
P.ensure_schema()

st.title("Student locations")
st.caption("Every student has a default pickup and drop-off location (address + map pin). Pins are only used to choose an approved "
           f"safe stop within {P.WALK_MAX_M} m walking distance — a home pin is never used as a bus stop. Any change needs school "
           "approval and every version is kept.")

ov = P.location_overview()
df = pd.DataFrame(ov)
pend = P.pending_locations()
m = st.columns(5)
m[0].metric("Students", len(df))
m[1].metric("Locations OK", int((df["Status"] == "OK").sum()) if not df.empty else 0)
m[2].metric("Missing", int(df["Status"].str.contains("missing").sum()) if not df.empty else 0)
m[3].metric("Invalid / no safe stop", int(df["Status"].str.contains("invalid|no safe stop").sum()) if not df.empty else 0)
m[4].metric("Waiting for approval", len(pend))

if "loc_jump" in st.session_state:      # set before the student pickers are drawn
    st.session_state.loc_sel = st.session_state.loc_pick_reg = st.session_state.loc_pick_hist = st.session_state.pop("loc_jump")
tabs = st.tabs(["Students", "Register / change", f"Approvals ({len(pend)})", "History"])

# ------------------------------------------------------------------ list
with tabs[0]:
    c = st.columns([1, 1, 1, 1.4, 2])
    buses = sorted(df["Bus"].dropna().unique()) if not df.empty else []
    fb = c[0].selectbox("Bus", ["All"] + list(buses), key="loc_bus")
    fbt = c[1].selectbox("Batch", ["All", 1, 2], key="loc_batch")
    fg = c[2].selectbox("Group", ["All", "Junior", "Senior"], key="loc_grp")
    fi = c[3].segmented_control("Show", ["All", "Issues only"], default="All", key="loc_issue")
    fs = c[4].text_input("Search name / code", key="loc_q")
    v = df.copy()
    if fb != "All":
        v = v[v["Bus"] == fb]
    if fbt != "All":
        v = v[v["Batch"] == fbt]
    if fg != "All":
        v = v[v["Group"] == fg]
    if fi == "Issues only":
        v = v[(v["Status"] != "OK") | (v["Pending"] > 0)]
    if fs:
        v = v[v["Student"].str.contains(fs, case=False) | v["Code"].str.contains(fs, case=False)]
    cols = ["Code", "Student", "Class", "Bus", "Batch", "Group", "Assigned stop", "Pickup", "Pickup stop", "Drop-off", "Drop-off stop",
            "Status", "Pending"]
    st.dataframe(v[cols] if not v.empty else v, hide_index=True, width="stretch",
                 column_config={"Status": st.column_config.TextColumn("Check", help="Checked before every departure"),
                                "Pending": st.column_config.NumberColumn("Pending changes")})
    st.caption("“Pickup stop” / “Drop-off stop” = the approved safe stop the planner will use, with the walking distance from the "
               "registered pin. Students with a missing or invalid location are flagged on the trip plan before departure and fall back to "
               "their assigned stop until corrected.")
    if not v.empty:
        st.download_button("⬇ CSV", v[cols].to_csv(index=False).encode(), file_name="student_locations.csv", mime="text/csv")


def loc_map(student, pick, drop, cand=None, height=330):
    """Registered pins vs. approved safe stops for one student."""
    stops = P.safe_stops()
    sdf = pd.DataFrame([{"lat": s["lat"], "lon": s["lon"], "label": f"Safe stop · {s['name']} ({s['bus_no'] or '—'})",
                         "color": [61, 79, 214, 220] if s["bus_id"] == student["bus_id"] else [140, 148, 170, 150]} for s in stops])
    layers = [pdk.Layer("ScatterplotLayer", sdf, get_position="[lon, lat]", get_radius=14, radius_min_pixels=5, radius_max_pixels=14,
                        get_fill_color="color", pickable=True),
              pdk.Layer("TextLayer", sdf.assign(short=sdf["label"].str.split("·").str[1].str.split("(").str[0].str.strip()), get_position="[lon, lat]",
                        get_text="short", get_size=11, get_pixel_offset=[0, -16], get_color=[26, 32, 54])]
    pins, links = [], []
    for loc, lab, col in ((pick, "Pickup", [46, 158, 91]), (drop, "Drop-off", [224, 122, 31]), (cand, "New pin", [217, 48, 37])):
        if loc and loc.get("lat") is not None and P._valid_qatar(loc["lat"], loc["lon"]):
            s, d = P.nearest_safe_stop(loc["lat"], loc["lon"], stops, student["bus_id"])
            pins.append({"lat": loc["lat"], "lon": loc["lon"], "color": col,
                         "label": f"{lab} pin · {loc.get('address') or ''}" + (f" → {s['name']} ({d:.0f} m walk)" if s else " → no safe stop")})
            if s:
                links.append({"path": [[loc["lon"], loc["lat"]], [s["lon"], s["lat"]]], "color": col})
    if pins:
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame(pins), get_position="[lon, lat]", get_radius=8, radius_min_pixels=6, radius_max_pixels=11,
                                get_fill_color="color", get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=2, pickable=True))
    if links:
        layers.insert(0, pdk.Layer("PathLayer", pd.DataFrame(links), get_path="path", get_color="color", width_min_pixels=2,
                                   get_dash_array=[4, 3]))
    centre = pins[0] if pins else (sdf.iloc[0].to_dict() if not sdf.empty else {"lat": db.school_pos()[0], "lon": db.school_pos()[1]})
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(latitude=centre["lat"], longitude=centre["lon"], zoom=15),
                             map_style="light", tooltip={"text": "{label}"}), height=height)
    st.caption("● Pickup pin · ● Drop-off pin · ● New pin · ● This bus's safe stops · ● Other safe stops · dashed = walk to the stop used")


def pick_student(key):
    opts = {r["student_id"]: f"{r['Student']} · {r['Code']} · {r['Bus'] or '—'} · {r['Status']}" for r in ov}
    if not opts:
        st.info("No students.")
        return None
    pre = st.session_state.get("loc_sel")
    ids = list(opts)
    sid = st.selectbox("Student", ids, index=ids.index(pre) if pre in ids else 0, format_func=opts.get, key=key)
    st.session_state.loc_sel = sid
    return db.q1("SELECT * FROM students WHERE id=?", (sid,))


# ------------------------------------------------------------------ register / change
with tabs[1]:
    stu = pick_student("loc_pick_reg")
    if stu:
        pick = P.current_location(stu["id"], "PICKUP")
        drop = P.current_location(stu["id"], "DROPOFF")
        chk = P.location_check(stu)
        left, right = st.columns([1, 1.15])
        with left:
            with st.container(border=True):
                st.markdown("**Current approved locations**")
                for kind, lab in (("PICKUP", "Pickup"), ("DROPOFF", "Drop-off")):
                    c_ = chk[kind]
                    loc = c_["loc"]
                    if not loc or loc["lat"] is None:
                        st.markdown(f"🔴 **{lab}:** not registered")
                        continue
                    same = " (same as pickup)" if kind == "DROPOFF" and loc.get("same_as_pickup") else ""
                    stop_txt = f"→ {c_['stop']['name']} · {c_['dist']:.0f} m walk" if c_["stop"] else ""
                    icon = "🟢" if c_["flag"] == "OK" else "🟠"
                    st.markdown(f"{icon} **{lab}{same}:** {loc['address'] or '—'}  \n"
                                f"<span style='color:#6B7390'>{loc['lat']:.6f}, {loc['lon']:.6f} · v{loc['version']} · {loc['notes'] or ''} "
                                f"{stop_txt}{'' if c_['flag'] == 'OK' else ' · ' + c_['flag']}</span>", unsafe_allow_html=True)
                if chk["pending"]:
                    st.warning(f"{chk['pending']} change(s) waiting for school approval.")
            guardians = db.q("""SELECT g.* FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id WHERE sg.student_id=?
                                ORDER BY sg.is_primary DESC""", (stu["id"],))
            with st.form("loc_form", border=True):
                st.markdown("**Submit a new default location**")
                kinds = st.segmented_control("Location for", ["Pickup", "Drop-off", "Both (same place)"], default="Both (same place)",
                                             key="loc_kind")
                base = (drop if kinds == "Drop-off" else pick) or {}
                addr = st.text_input("Address", value=base.get("address") or "", placeholder="Villa / building, street, zone, city")
                c1, c2 = st.columns(2)
                lat = c1.number_input("Latitude", value=float(base.get("lat") or db.school_pos()[0]), format="%.6f", step=0.0001)
                lon = c2.number_input("Longitude", value=float(base.get("lon") or db.school_pos()[1]), format="%.6f", step=0.0001)
                notes = st.text_input("Notes (gate, landmark, who meets the child)", value=base.get("notes") or "")
                g_ids = [g["id"] for g in guardians] or [None]
                gid = st.selectbox("Linked guardian", g_ids, format_func=lambda i: next((f"{g['name']} · {g['relation'] or ''} · {g['phone'] or ''}"
                                                                                     for g in guardians if g["id"] == i), "—"))
                reason = st.text_input("Reason for the change", placeholder="e.g. family moved, afternoons at grandparents")
                preview = st.form_submit_button("Check pin")
                submit = st.form_submit_button("Submit for school approval", type="primary")
            cand = {"lat": lat, "lon": lon, "address": addr}
            if preview or submit:
                if not P._valid_qatar(lat, lon):
                    st.error("This pin is outside Qatar — please correct the coordinates.")
                else:
                    s, d = P.nearest_safe_stop(lat, lon, P.safe_stops(), stu["bus_id"])
                    if s:
                        st.success(f"Nearest approved safe stop: **{s['name']}** ({s['bus_no'] or '—'}) · {d:.0f} m walk.")
                    else:
                        st.warning(f"No approved safe stop within {P.WALK_MAX_M} m. The location can still be submitted, but the "
                                   "transport office must approve a new safe stop before the planner can use it.")
            if submit:
                if not addr.strip() or not reason.strip():
                    st.error("Address and reason are required.")
                elif not P._valid_qatar(lat, lon):
                    pass
                else:
                    if kinds in ("Pickup", "Both (same place)"):
                        P.request_location(stu["id"], "PICKUP", addr.strip(), lat, lon, notes, gid, False, reason, user["username"], role)
                    if kinds == "Drop-off":
                        P.request_location(stu["id"], "DROPOFF", addr.strip(), lat, lon, notes, gid, False, reason, user["username"], role)
                    elif kinds == "Both (same place)":
                        P.request_location(stu["id"], "DROPOFF", addr.strip(), None, None, "Same as pickup", gid, True, reason,
                                           user["username"], role)
                    st.session_state.loc_msg = "Submitted — waiting for school approval (Approvals tab)."
                    st.rerun()
            if st.session_state.get("loc_msg"):
                st.success(st.session_state.pop("loc_msg"))
        with right:
            loc_map(stu, pick, drop, cand if (preview or submit) else None)

# ------------------------------------------------------------------ approvals
with tabs[2]:
    if not pend:
        st.success("No location changes waiting for approval.")
    if pend and not can_approve:
        st.info("Location changes are approved by the transport manager or an administrator.")
    for r in pend:
        stu = db.q1("SELECT * FROM students WHERE id=?", (r["student_id"],))
        cur = P.current_location(r["student_id"], r["kind"])
        with st.container(border=True):
            a, b = st.columns([1.2, 1])
            with a:
                st.markdown(f"**{r['name']}** · {r['code']} · {r['kind'].title()} v{r['version']}  \n"
                            f"Requested by {r['requested_by']} · {r['requested_ts']} · reason: *{r['reason'] or '—'}*")
                new_txt = "Same as pickup" if r["same_as_pickup"] else f"{r['address']} ({r['lat']}, {r['lon']})"
                old_txt = (cur["address"] or "—") if cur else "not registered"
                st.markdown(f"Current: {old_txt}  \nNew: **{new_txt}**")
                if not r["same_as_pickup"] and r["lat"] is not None:
                    s, d = P.nearest_safe_stop(r["lat"], r["lon"], P.safe_stops(), stu["bus_id"])
                    st.caption(f"Nearest safe stop: {s['name']} · {d:.0f} m" if s else f"⚠ No safe stop within {P.WALK_MAX_M} m")
                if can_approve:
                    note = st.text_input("Decision note", key=f"ln{r['id']}")
                    c1, c2 = st.columns(2)
                    if c1.button("Approve", key=f"la{r['id']}", type="primary", icon=":material/check:"):
                        P.decide_location(r["id"], True, user["username"], role, note)
                        st.session_state.loc_jump = r["student_id"]      # Register / History tabs open on this student
                        st.rerun()
                    if c2.button("Reject", key=f"lr{r['id']}", icon=":material/close:"):
                        P.decide_location(r["id"], False, user["username"], role, note)
                        st.rerun()
            with b:
                if not r["same_as_pickup"] and r["lat"] is not None:
                    loc_map(stu, cur, None, {"lat": r["lat"], "lon": r["lon"], "address": r["address"]}, height=230)
    st.caption("Approved changes apply from the next plan; if a trip for today is already planned, the trip planning page offers a revised "
               "route for approval.")

# ------------------------------------------------------------------ history
with tabs[3]:
    stu = pick_student("loc_pick_hist")
    if stu:
        h = P.location_history(stu["id"])
        if not h:
            st.info("No locations registered.")
        else:
            hd = pd.DataFrame(h)
            hd["address"] = hd.apply(lambda r: "Same as pickup" if r["same_as_pickup"] else r["address"], axis=1)
            st.dataframe(hd[["kind", "version", "status", "address", "lat", "lon", "notes", "reason", "requested_by", "requested_ts",
                             "decided_by", "decided_ts", "decision_note"]], hide_index=True, width="stretch")
