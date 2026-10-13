"""Office: the 30 MVP report views with filters, definitions, drill-down, CSV, printable PDF and data freshness."""
from datetime import date, timedelta

import streamlit as st

import db
import engine as E
import reports as R
from db import TRIPS
from ui import lines_table

st.title("📊 Reports")
fresh, pend = R.freshness()
st.caption(f"Data as of **{fresh}** (latest scan received) · trips pending sync: **{pend}** · generated {db.ts()}")

span = db.q1("SELECT MIN(date) a, MAX(date) b FROM trip_runs")
d_hi = date.fromisoformat(max(span["b"] or db.today(), db.today()))
d_lo = date.fromisoformat(span["a"]) if span["a"] else d_hi - timedelta(days=7)

cats = sorted({r[1] for r in R.REPORTS}, key=["Operations", "Safety", "Handover", "Students", "Parents", "Data quality", "Setup"].index)
c1, c2 = st.columns([1, 2])
cat = c1.selectbox("Category", ["All"] + cats)
opts = [r for r in R.REPORTS if cat == "All" or r[1] == cat]
key = c2.selectbox("Report", [r[0] for r in opts], format_func=lambda k: f"{R.REPORTS.index(R.BY_KEY[k]) + 1}. {R.BY_KEY[k][2]}")
_, category, title, definition, fn, drill = R.BY_KEY[key]

with st.container(border=True):
    f1, f2, f3, f4, f5, f6 = st.columns([1.1, 1.1, 1, 1, 1, 1.4])
    d1 = f1.date_input("From", value=d_lo)
    d2 = f2.date_input("To", value=d_hi)
    trips = f3.multiselect("Trip", list(TRIPS))
    batches = f4.multiselect("Batch", [1, 2])
    groups = f5.multiselect("Group", ["Junior", "Senior"])
    bl = db.buses()
    buses = f6.multiselect("Bus", [b["id"] for b in bl], format_func=lambda i: next(b["bus_no"] for b in bl if b["id"] == i))
    f = {"d1": d1.isoformat(), "d2": d2.isoformat(), "trips": trips, "batches": batches, "groups": groups, "buses": buses}
    if key == "journey":
        st_list = db.q("SELECT id, code, name FROM students ORDER BY name")
        f["student"] = st.selectbox("Student", [s["id"] for s in st_list], format_func=lambda i: next(f"{s['name']} ({s['code']})" for s in st_list if s["id"] == i))

st.subheader(title)
st.caption(f"**Definition:** {definition}")
out = fn(f)
if out.attrs.get("note"):
    st.caption(out.attrs["note"])
if out.empty:
    st.info("No data for these filters.")
else:
    st.dataframe(out, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(out)), key=f"rep_{key}",
                 on_select="rerun" if drill else "ignore", selection_mode="single-row")
    ftxt = (f"{f['d1']} to {f['d2']}; trips {trips or 'all'}; batch {batches or 'all'}; group {groups or 'all'}; "
            f"buses {[b['bus_no'] for b in bl if b['id'] in buses] or 'all'}")
    c1, c2, _ = st.columns([1, 1, 3])
    c1.download_button("⬇ CSV", out.to_csv(index=False).encode("utf-8-sig"), file_name=f"{key}_{f['d1']}_{f['d2']}.csv", mime="text/csv")
    c2.download_button("🖨 PDF", R.to_pdf(title, definition, ftxt, out), file_name=f"{key}_{f['d1']}_{f['d2']}.pdf", mime="application/pdf")
    st.caption(f"{len(out):,} rows" + (" · select a row to drill down" if drill else ""))

    try:
        rows = st.session_state[f"rep_{key}"]["selection"]["rows"]
    except (KeyError, TypeError):
        rows = []
    if drill and rows and rows[0] < len(out):
        row = out.iloc[rows[0]]
        with st.container(border=True):
            if drill == "run" and "Run" in out:
                rid = int(row["Run"])
                run = E.get_run(rid)
                st.markdown(f"**Drill-down: {db.bus(run['bus_id'])['bus_no']} · {run['date']} · Trip {run['trip_no']}** ({run['status']})")
                lines_table(E.run_lines(rid), ("name", "code", "class", "batch", "grp", "purpose", "stop_name", "status", "entry_ts", "exit_ts",
                                               "handover", "recipient_name", "received_by"))
            elif drill == "student" and "Student_ID" in out:
                s = db.q1("SELECT id, name FROM students WHERE code=?", (row["Student_ID"],))
                st.markdown(f"**Drill-down: journey record — {s['name']}**")
                st.dataframe(R.r_journey(dict(f, student=s["id"])), hide_index=True, width="stretch")
