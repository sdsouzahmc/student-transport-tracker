"""Office: the predefined daily list for every bus and trip (who is expected to travel), sent to the bus devices."""
from datetime import date

import pandas as pd
import streamlit as st

import db
import engine as E
import reports as R
from db import TRIPS
from ui import lines_table

st.title("📋 Daily bus lists")
st.caption("Every morning each bus receives the list of children expected on each of its trips. Declared absences and approved "
           "stop / bus changes are applied automatically; parents' 'not coming today' updates the list immediately. "
           "Crew scan against this list — a child who is not on it is flagged as wrong bus.")
c1, c2 = st.columns([1, 2])
d = c1.date_input("Date", value=date.fromisoformat(db.today())).isoformat()
bl = db.buses()

# overview: buses x trips
rows = []
for b in bl:
    row = {"Bus": b["bus_no"], "Capacity": b["capacity"]}
    for n in TRIPS:
        run = db.q1("SELECT id FROM trip_runs WHERE bus_id=? AND date=? AND trip_no=?", (b["id"], d, n))
        lines = E.run_lines(run["id"]) if run else E.manifest_for(b["id"], d, n)
        exp = sum(1 for ln in lines if ln["status"] != "ABSENT_DECLARED")
        ab = sum(1 for ln in lines if ln["status"] == "ABSENT_DECLARED")
        row[f"Trip {n}"] = f"{exp}" + (f" (+{ab} absent)" if ab else "") + (" ✔" if run else "")
    rows.append(row)
st.subheader(f"All buses — {d}")
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
st.caption("Numbers = children expected to travel; ✔ = trip already started (list frozen into the trip manifest).")

st.subheader("List for one bus")
c1, c2 = st.columns(2)
bus_id = c1.selectbox("Bus", [b["id"] for b in bl], format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i))
trip = c2.selectbox("Trip", list(TRIPS), format_func=lambda n: f"Trip {n} · {TRIPS[n]['window'][0]}–{TRIPS[n]['window'][1]} · {TRIPS[n]['label']}")
run = db.q1("SELECT id, status FROM trip_runs WHERE bus_id=? AND date=? AND trip_no=?", (bus_id, d, trip))
if run:
    lines = E.run_lines(run["id"])
    st.caption(f"Trip started ({run['status']}) — showing the live manifest.")
else:
    lines = E.manifest_for(bus_id, d, trip)
    for ln in lines:
        s = db.stop(ln["stop_id"]) if ln["stop_id"] is not None else None
        ln["stop_name"], ln["stop_seq"] = (s["name"], s["seq"]) if s else ("", 0)
    lines.sort(key=lambda ln: (ln["purpose"] != "DROPOFF", ln["stop_seq"], ln["name"]))
df = pd.DataFrame(lines)
if not df.empty:
    cnt = df.groupby(["batch", "grp", "purpose"]).agg(Planned=("student_id", "count"),
                                                      Absent=("status", lambda s: (s == "ABSENT_DECLARED").sum())).reset_index()
    st.dataframe(cnt.rename(columns={"batch": "Batch", "grp": "Group", "purpose": "Purpose"}), hide_index=True)
lines_table(lines, ("name", "code", "class", "batch", "grp", "purpose", "stop_name", "status"))
if lines:
    bno = next(b["bus_no"] for b in bl if b["id"] == bus_id)
    st.download_button("🖨 Print list (PDF)", R.roster_pdf(lines, f"{bno} · {d} · Trip {trip} list"), file_name=f"{bno}_{d}_trip{trip}.pdf",
                       mime="application/pdf")
    st.download_button("⬇ CSV", df.to_csv(index=False).encode(), file_name=f"{bno}_{d}_trip{trip}.csv", mime="text/csv")
