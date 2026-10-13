"""The 30 MVP report views (spec §8): filters by date, trip, batch, group, bus; definitions; CSV; printable PDF; data freshness."""
import io
from datetime import datetime

import pandas as pd

import db
from db import TRIPS

STATUS_TEXT = {"EXPECTED": "Expected", "ONBOARD": "On board", "COMPLETED": "Completed", "RETURNED": "Returned to school",
               "ABSENT_DECLARED": "Declared absent", "NO_SHOW": "No show", "TRANSFER_PENDING": "Awaiting transfer",
               "TRANSFERRED_OUT": "Transferred out", "CANCELLED": "Transport cancelled"}


def df(sql, params=()):
    return pd.DataFrame(db.q(sql, params))


def run_filter(f, r="r", m=None):
    """WHERE fragment for trip_runs alias r and (optionally) manifest alias m."""
    w, p = [f"{r}.date BETWEEN ? AND ?"], [f["d1"], f["d2"]]
    for col, key in ((f"{r}.trip_no", "trips"), (f"{r}.bus_id", "buses")):
        if f.get(key):
            w.append(f"{col} IN ({','.join('?' * len(f[key]))})")
            p += list(f[key])
    if m:
        for col, key in ((f"{m}.batch", "batches"), (f"{m}.grp", "groups")):
            if f.get(key):
                w.append(f"{col} IN ({','.join('?' * len(f[key]))})")
                p += list(f[key])
    return " AND ".join(w), p


def day_filter(f, col):
    return f"substr({col},1,10) BETWEEN ? AND ?", [f["d1"], f["d2"]]


def bus_filter(f, col):
    if not f.get("buses"):
        return "1=1", []
    return f"{col} IN ({','.join('?' * len(f['buses']))})", list(f["buses"])


def _status(frame, col="Status"):
    if not frame.empty and col in frame:
        frame[col] = frame[col].map(lambda s: STATUS_TEXT.get(s, s))
    return frame


def _mins(a, b):
    try:
        return round((datetime.strptime(b, "%Y-%m-%d %H:%M:%S") - datetime.strptime(a, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ report functions
def r_live(f):
    w, p = run_filter(f)
    return df(f"""SELECT r.id AS Run, b.bus_no AS Bus, r.trip_no AS Trip, r.status AS "Trip status", substr(r.start_ts,12,5) AS Started,
        sv.name AS Supervisor,
        SUM(m.status='ONBOARD') AS "On board", SUM(m.status='EXPECTED') AS "Still expected", SUM(m.status IN ('COMPLETED','RETURNED')) AS Completed,
        SUM(m.status='TRANSFER_PENDING') AS "Awaiting transfer",
        (SELECT substr(MAX(e.captured_ts),12,8) FROM events e WHERE e.run_id=r.id) AS "Last scan"
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id LEFT JOIN staff sv ON sv.id=r.supervisor_id LEFT JOIN manifest m ON m.run_id=r.id
        WHERE r.status IN ('IN_PROGRESS','PENDING_SYNC') AND {w} GROUP BY r.id ORDER BY b.bus_no""", p)


def r_trip_summary(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.id AS Run, r.date AS Date, r.trip_no AS Trip, b.bus_no AS Bus, r.status AS "Trip status", COUNT(m.id) AS Planned,
        SUM(m.status='ABSENT_DECLARED') AS Absent, SUM(m.status='NO_SHOW') AS "No show", SUM(m.status='COMPLETED') AS Completed,
        SUM(m.status='RETURNED') AS "Returned to school", SUM(m.status IN ('EXPECTED','ONBOARD','TRANSFER_PENDING')) AS Unaccounted
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id LEFT JOIN manifest m ON m.run_id=r.id WHERE {w}
        GROUP BY r.id ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)


def r_onboard(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.id AS Run, b.bus_no AS Bus, r.trip_no AS Trip, s.code AS Student_ID, s.name AS Student, s.class_name||s.section AS Class,
        m.batch AS Batch, m.grp AS "Group", m.purpose AS Purpose, substr(m.entry_ts,12,5) AS Boarded, st.name AS "Destination stop"
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
        LEFT JOIN stops st ON st.id=m.stop_id WHERE m.status='ONBOARD' AND {w} ORDER BY b.bus_no, s.name""", p)


def r_unaccounted(f):
    w, p = run_filter(f, m="m")
    out = df(f"""SELECT r.id AS Run, r.date AS Date, b.bus_no AS Bus, r.trip_no AS Trip, r.status AS "Trip status", s.code AS Student_ID,
        s.name AS Student, m.purpose AS Purpose, m.status AS Status, substr(m.entry_ts,12,5) AS Boarded, m.note AS Note
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
        WHERE m.status IN ('EXPECTED','ONBOARD','TRANSFER_PENDING') AND r.status<>'PLANNED' AND {w}
        ORDER BY (m.status='TRANSFER_PENDING') DESC, (m.status='ONBOARD') DESC, b.bus_no""", p)
    return _status(out)


def r_reconciliation(f):
    w, p = run_filter(f)
    out = df(f"""SELECT r.id AS Run, r.date AS Date, b.bus_no AS Bus, r.trip_no AS Trip, r.status AS "Trip status",
        COUNT(m.id) AS Planned, SUM(m.status IN ('COMPLETED','RETURNED','ABSENT_DECLARED','NO_SHOW','TRANSFERRED_OUT','CANCELLED')) AS "Accounted for",
        SUM(m.status IN ('ONBOARD','TRANSFER_PENDING')) AS "On board at close",
        r.sweep_by AS "Sweep by", substr(r.sweep_ts,12,5) AS "Sweep at", substr(r.start_ts,12,5) AS Started,
        printf('%.5f, %.5f', r.start_lat, r.start_lon) AS "Start GPS", substr(r.end_ts,12,5) AS Ended,
        printf('%.5f, %.5f', r.end_lat, r.end_lon) AS "End GPS",
        (SELECT name FROM staff WHERE id=r.final_driver_id) || ' / ' || (SELECT name FROM staff WHERE id=r.final_supervisor_id) || ' / ' ||
        (SELECT name FROM staff WHERE id=r.final_caretaker_id) AS "Final crew", r.closed_by AS "Closed by"
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id LEFT JOIN manifest m ON m.run_id=r.id
        WHERE r.status IN ('CLOSED','PENDING_SYNC') AND {w} GROUP BY r.id ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)
    if not out.empty:
        out["Reconciled"] = (out["Planned"] == out["Accounted for"]).map({True: "Yes", False: "NO"})
    return out


def r_punctuality(f):
    w, p = run_filter(f)
    out = df(f"""SELECT r.id AS Run, r.date AS Date, b.bus_no AS Bus, r.trip_no AS Trip, r.start_ts, r.end_ts
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.start_ts IS NOT NULL AND {w} ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)
    if out.empty:
        return out
    out["Window"] = out["Trip"].map(lambda n: "–".join(TRIPS[n]["window"]))
    out["Started"] = out["start_ts"].str[11:16]
    out["Ended"] = out["end_ts"].str[11:16]
    out["Start delay (min)"] = [_mins(f"{d} {TRIPS[n]['window'][0]}:00", s) for d, n, s in zip(out["Date"], out["Trip"], out["start_ts"])]
    out["Finished after window (min)"] = [max(0, _mins(f"{d} {TRIPS[n]['window'][1]}:00", e) or 0) if e else None
                                          for d, n, e in zip(out["Date"], out["Trip"], out["end_ts"])]
    out["On time"] = out["Start delay (min)"].map(lambda v: "Yes" if v is not None and v <= 10 else "Late")
    return out.drop(columns=["start_ts", "end_ts"])


def r_crew(f):
    w, p = run_filter(f)
    out = df(f"""SELECT r.id AS Run, r.date AS Date, b.bus_no AS Bus, r.trip_no AS Trip,
        d.name AS "Start driver", s.name AS "Start supervisor", c.name AS "Start care-taker",
        fd.name AS "Final driver", fs.name AS "Final supervisor", fc.name AS "Final care-taker", r.started_by AS "Started by"
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id LEFT JOIN staff d ON d.id=r.driver_id LEFT JOIN staff s ON s.id=r.supervisor_id
        LEFT JOIN staff c ON c.id=r.caretaker_id LEFT JOIN staff fd ON fd.id=r.final_driver_id LEFT JOIN staff fs ON fs.id=r.final_supervisor_id
        LEFT JOIN staff fc ON fc.id=r.final_caretaker_id WHERE {w} ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)
    if not out.empty:
        out["Crew changed"] = [("Yes" if any(a != b and b for a, b in ((r["Start driver"], r["Final driver"]), (r["Start supervisor"], r["Final supervisor"]),
                                                                    (r["Start care-taker"], r["Final care-taker"]))) else "No") for _, r in out.iterrows()]
    return out


def _peak(lines):
    ev = []
    for e, x_ in lines:
        if e:
            ev.append((e, 1))
            if x_:
                ev.append((x_, -1))
    ev.sort()
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def r_capacity(f):
    w, p = run_filter(f)
    runs = db.q(f"""SELECT r.id, r.date, r.trip_no, b.bus_no, b.capacity FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE {w}
                    ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)
    rows = []
    for r in runs:
        lines = [(m["entry_ts"], m["exit_ts"]) for m in db.q("SELECT entry_ts, exit_ts FROM manifest WHERE run_id=?", (r["id"],))]
        pk = _peak(lines)
        rows.append({"Run": r["id"], "Date": r["date"], "Trip": r["trip_no"], "Bus": r["bus_no"], "Capacity": r["capacity"],
                     "Students carried": sum(1 for e, _ in lines if e), "Peak on board": pk, "Peak load %": round(100 * pk / r["capacity"]),
                     "Over capacity": "YES" if pk > r["capacity"] else ""})
    return pd.DataFrame(rows)


def r_duration(f):
    w, p = run_filter(f, m="m")
    out = df(f"""SELECT b.bus_no AS Bus, r.trip_no AS Trip, m.purpose AS Purpose, m.entry_ts, m.exit_ts
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id
        WHERE m.entry_ts IS NOT NULL AND m.exit_ts IS NOT NULL AND {w}""", p)
    if out.empty:
        return out
    out["min"] = [_mins(a, b) for a, b in zip(out["entry_ts"], out["exit_ts"])]
    g = out.groupby(["Bus", "Trip", "Purpose"])["min"].agg(["count", "mean", "max"]).reset_index()
    g.columns = ["Bus", "Trip", "Purpose", "Journeys", "Average ride (min)", "Longest ride (min)"]
    g["Average ride (min)"] = g["Average ride (min)"].round(1)
    g["Over 60 min"] = g["Longest ride (min)"].map(lambda v: "YES" if v > 60 else "")
    return g


def r_stops(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT b.bus_no AS Bus, st.seq AS Seq, st.name AS Stop, SUM(m.purpose='PICKUP' AND m.entry_ts IS NOT NULL) AS "Picked up",
        SUM(m.purpose='DROPOFF' AND m.handover='HOME_RELEASE') AS Released, SUM(m.status='NO_SHOW') AS "No show",
        SUM(m.status='ABSENT_DECLARED') AS Absent, SUM(m.handover='RETURNED_TO_SCHOOL') AS "Not released (returned)"
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN stops st ON st.id=m.stop_id
        WHERE {w} GROUP BY st.id ORDER BY b.bus_no, st.seq""", p)


def r_attendance(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT s.code AS Student_ID, s.name AS Student, s.class_name||s.section AS Class, s.batch AS Batch, s.grp AS "Group",
        b.bus_no AS "Home bus", COUNT(m.id) AS "Planned journeys", SUM(m.status IN ('COMPLETED','RETURNED')) AS Travelled,
        SUM(m.status='ABSENT_DECLARED') AS Absent, SUM(m.status='NO_SHOW') AS "No show"
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN students s ON s.id=m.student_id JOIN buses b ON b.id=s.bus_id
        WHERE {w} GROUP BY s.id ORDER BY "No show" DESC, s.name""", p)


def r_receipt(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.date AS Date, r.trip_no AS Trip, b.bus_no AS Bus, s.code AS Student_ID, s.name AS Student, m.batch AS Batch,
        m.grp AS "Group", substr(m.exit_ts,12,8) AS "Received at", m.received_by AS "Received by"
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
        WHERE m.handover='SCHOOL_RECEIPT' AND {w} ORDER BY r.date DESC, m.exit_ts DESC""", p)


def r_release(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.date AS Date, r.trip_no AS Trip, b.bus_no AS Bus, s.code AS Student_ID, s.name AS Student, st.name AS Stop,
        substr(m.exit_ts,12,8) AS "Released at", m.recipient_name AS "Handed to", m.recipient_relation AS Relation
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
        LEFT JOIN stops st ON st.id=m.stop_id WHERE m.handover='HOME_RELEASE' AND {w} ORDER BY r.date DESC, m.exit_ts DESC""", p)


def r_returned(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.date AS Date, r.trip_no AS Trip, b.bus_no AS Bus, s.code AS Student_ID, s.name AS Student, st.name AS "Planned stop",
        substr(m.exit_ts,12,5) AS "Back at school", m.received_by AS "Custody with", m.handover AS Reason
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id JOIN students s ON s.id=m.student_id
        LEFT JOIN stops st ON st.id=m.stop_id WHERE m.status='RETURNED' AND {w} ORDER BY r.date DESC""", p)


def r_abs_vs_noshow(f):
    w, p = run_filter(f, m="m")
    out = df(f"""SELECT r.date AS Date, r.trip_no AS Trip, m.batch AS Batch, m.grp AS "Group", COUNT(m.id) AS Planned,
        SUM(m.status='ABSENT_DECLARED') AS "Declared absent", SUM(m.status='NO_SHOW') AS "No show (not declared)"
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id WHERE {w} GROUP BY r.date, r.trip_no, m.batch, m.grp
        ORDER BY r.date DESC, r.trip_no""", p)
    if not out.empty:
        out["Undeclared share %"] = (100 * out["No show (not declared)"] / (out["Declared absent"] + out["No show (not declared)"]).replace(0, float("nan"))).round(0)
    return out


def r_incidents(f):
    w, p = day_filter(f, "i.ts")
    wb, pb = bus_filter(f, "i.bus_id")
    return df(f"""SELECT i.id AS ID, i.ts AS Time, b.bus_no AS Bus, r.trip_no AS Trip, i.kind AS Type, i.severity AS Severity, s.name AS Student,
        i.status AS Status, i.assigned_to AS "Assigned to", i.details AS Details, i.resolution AS Resolution,
        (SELECT COUNT(*) FROM incident_contacts ic WHERE ic.incident_id=i.id) AS "Contact attempts"
        FROM incidents i LEFT JOIN buses b ON b.id=i.bus_id LEFT JOIN trip_runs r ON r.id=i.run_id LEFT JOIN students s ON s.id=i.student_id
        WHERE {w} AND {wb} ORDER BY i.id DESC""", p + pb)


def r_sweep(f):
    w, p = run_filter(f)
    out = df(f"""SELECT r.id AS Run, r.date AS Date, b.bus_no AS Bus, r.trip_no AS Trip, r.status AS "Trip status", r.sweep_by AS "Sweep by",
        r.sweep_ts, (SELECT MAX(m.exit_ts) FROM manifest m WHERE m.run_id=r.id) AS last_exit
        FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.status<>'PLANNED' AND {w} ORDER BY r.date DESC, r.trip_no, b.bus_no""", p)
    if out.empty:
        return out
    out["Sweep at"] = out["sweep_ts"].str[11:16]
    out["Minutes after last child"] = [_mins(a, b) if a and b else None for a, b in zip(out["last_exit"], out["sweep_ts"])]
    out["Compliant"] = out.apply(lambda r: "Yes" if r["Sweep by"] else ("Open" if r["Trip status"] == "IN_PROGRESS" else "NO"), axis=1)
    return out.drop(columns=["sweep_ts", "last_exit"])


def _reason_cat(r):
    r = (r or "").lower()
    for key, cat in (("duplicate", "Duplicate scan"), ("revoked", "Revoked badge"), ("invalid badge", "Invalid badge"),
                     ("never checked in", "Exit without entry"), ("drivers must not", "Driver attempted scan"), ("manual entry", "Manual entry incomplete"),
                     ("full", "Capacity limit"), ("not scheduled", "Not on this trip"), ("not an authorized", "Unauthorized recipient"),
                     ("no approved recipient", "No approved recipient"), ("transfer", "Awaiting transfer"), ("device", "Revoked device"),
                     ("pickup", "Pickup checked out away from school"), ("not in progress", "Trip not open")):
        if key in r:
            return cat
    return "Other"


def r_scan_errors(f):
    w, p = day_filter(f, "e.captured_ts")
    wb, pb = bus_filter(f, "r.bus_id")
    out = df(f"""SELECT e.captured_ts AS Time, b.bus_no AS Bus, r.trip_no AS Trip, e.kind AS Action, s.name AS Student, e.badge_code AS Badge,
        e.reason AS Reason, e.operator AS Operator, e.method AS Method, e.result AS Result
        FROM events e LEFT JOIN trip_runs r ON r.id=e.run_id LEFT JOIN buses b ON b.id=r.bus_id LEFT JOIN students s ON s.id=e.student_id
        WHERE e.result IN ('REJECTED','FLAGGED') AND {w} AND {wb} ORDER BY e.id DESC""", p + pb)
    if not out.empty:
        out.insert(6, "Category", out["Reason"].map(_reason_cat))
    return out


def r_corrections(f):
    w, p = day_filter(f, "c.created_at")
    return df(f"""SELECT c.id AS ID, c.created_at AS Requested, s.name AS Student, b.bus_no AS Bus, r.trip_no AS Trip, c.field AS Field,
        c.old_value AS "Original value", c.new_value AS "Corrected value", c.reason AS Reason, c.requested_by AS "Requested by",
        c.status AS Status, c.decided_by AS "Decided by"
        FROM corrections c JOIN manifest m ON m.id=c.manifest_id JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id
        JOIN students s ON s.id=m.student_id WHERE {w} ORDER BY c.id DESC""", p)


def r_sync(f):
    w, p = day_filter(f, "e.captured_ts")
    wb, pb = bus_filter(f, "r.bus_id")
    out = df(f"""SELECT e.captured_ts AS "Captured on device", e.received_ts AS "Received by server", b.bus_no AS Bus, r.trip_no AS Trip,
        s.name AS Student, e.kind AS Action, e.connectivity AS "Captured", e.sync_state AS "Sync state", e.clock_flag AS "Clock issue", e.result AS Result
        FROM events e LEFT JOIN trip_runs r ON r.id=e.run_id LEFT JOIN buses b ON b.id=r.bus_id LEFT JOIN students s ON s.id=e.student_id
        WHERE (e.received_ts > datetime(e.captured_ts, '+60 seconds') OR e.connectivity='Offline' OR e.sync_state<>'SYNCED' OR e.clock_flag IS NOT NULL) AND {w} AND {wb}
        ORDER BY e.id DESC""", p + pb)
    if not out.empty:
        out.insert(2, "Delay (min)", [_mins(a, b) for a, b in zip(out["Captured on device"], out["Received by server"])])
    pend = db.q("SELECT COUNT(*) n FROM trip_runs WHERE status='PENDING_SYNC'")[0]["n"]
    out.attrs["note"] = f"Trips closed offline and still Pending Sync: {pend}"
    return out


def r_gps(f):
    w, p = day_filter(f, "e.captured_ts")
    wb, pb = bus_filter(f, "r.bus_id")
    out = df(f"""SELECT b.bus_no AS Bus, COUNT(*) AS Scans, SUM(e.gps_ok=0) AS "No GPS fix", ROUND(AVG(e.accuracy),1) AS "Avg accuracy (m)",
        SUM(e.accuracy>20) AS "Poor accuracy (>20 m)"
        FROM events e JOIN trip_runs r ON r.id=e.run_id JOIN buses b ON b.id=r.bus_id WHERE e.result<>'REJECTED' AND {w} AND {wb}
        GROUP BY b.id ORDER BY b.bus_no""", p + pb)
    if not out.empty:
        out["No GPS %"] = (100 * out["No GPS fix"] / out["Scans"]).round(1)
    return out


def r_notifications(f):
    w, p = day_filter(f, "n.ts")
    return df(f"""SELECT n.kind AS Type, n.channel AS Channel, COUNT(*) AS Messages, SUM(n.status='SENT') AS Delivered,
        SUM(n.status='FAILED') AS Failed, SUM(n.attempts>1) AS Retried, SUM(n.delayed_offline) AS "Delayed (offline)"
        FROM notifications n WHERE {w} GROUP BY n.kind, n.channel ORDER BY n.kind, n.channel""", p)


def r_badges(f):
    return df("""SELECT s.code AS Student_ID, s.name AS Student, b.code AS Badge, b.status AS Status, b.issued_at AS Issued,
        b.revoked_at AS Revoked, b.reason AS Reason, b.replaced_by AS "Replaced by",
        (SELECT COUNT(*) FROM events e WHERE e.badge_code=b.code AND b.status='REVOKED') AS "Scan attempts with this badge"
        FROM badges b JOIN students s ON s.id=b.student_id WHERE b.status<>'ACTIVE' OR b.issued_at >= ?
        ORDER BY b.revoked_at DESC""", (f["d1"],))


def r_audit(f):
    w, p = day_filter(f, "a.ts")
    return df(f"""SELECT a.ts AS Time, a.username AS User, a.role AS Role, a.action AS Action, a.entity AS Entity, a.entity_id AS "Entity ID",
        a.details AS Details FROM audit a WHERE {w} ORDER BY a.id DESC LIMIT 5000""", p)


def r_wrong(f):
    w, p = day_filter(f, "i.ts")
    wb, pb = bus_filter(f, "i.bus_id")
    return df(f"""SELECT i.ts AS Time, i.kind AS Type, b.bus_no AS Bus, r.trip_no AS Trip, s.name AS Student, i.details AS Details, i.status AS Status,
        i.resolution AS Resolution FROM incidents i LEFT JOIN buses b ON b.id=i.bus_id LEFT JOIN trip_runs r ON r.id=i.run_id
        LEFT JOIN students s ON s.id=i.student_id WHERE i.kind IN ('WRONG_BUS','WRONG_STOP') AND {w} AND {wb} ORDER BY i.id DESC""", p + pb)


def r_manual(f):
    w, p = day_filter(f, "e.captured_ts")
    wb, pb = bus_filter(f, "r.bus_id")
    return df(f"""SELECT e.captured_ts AS Time, b.bus_no AS Bus, r.trip_no AS Trip, s.name AS Student, e.kind AS Action, e.manual_reason AS Reason,
        e.operator AS "Entered by", e.verified_by AS "Verified by", e.result AS Result
        FROM events e JOIN trip_runs r ON r.id=e.run_id JOIN buses b ON b.id=r.bus_id LEFT JOIN students s ON s.id=e.student_id
        WHERE e.method='MANUAL' AND {w} AND {wb} ORDER BY e.id DESC""", p + pb)


def r_overcap(f):
    cap = r_capacity(f)
    if cap.empty:
        return cap
    w, p = day_filter(f, "i.ts")
    ov = db.q(f"SELECT run_id, COUNT(*) n, GROUP_CONCAT(details, ' | ') d FROM incidents i WHERE kind='OVERCAPACITY' AND {w} GROUP BY run_id", p)
    ovm = {o["run_id"]: o for o in ov}
    cap["Override incidents"] = cap["Run"].map(lambda r: ovm.get(r, {}).get("n", 0))
    cap["Override reasons"] = cap["Run"].map(lambda r: ovm.get(r, {}).get("d", ""))
    return cap[(cap["Peak load %"] >= 90) | (cap["Override incidents"] > 0)].reset_index(drop=True)


def r_changes(f):
    w, p = "c.date BETWEEN ? AND ?", [f["d1"], f["d2"]]
    return df(f"""SELECT c.id AS ID, c.date AS "For date", COALESCE(c.trip_no,'All') AS Trip, s.name AS Student, c.kind AS Type,
        COALESCE(st.name, nb.bus_no, c.recipient_name) AS "New value", c.note AS Note, c.requested_by AS "Requested by", c.status AS Status,
        c.decided_by AS "Decided by" FROM change_requests c JOIN students s ON s.id=c.student_id LEFT JOIN stops st ON st.id=c.new_stop_id
        LEFT JOIN buses nb ON nb.id=c.new_bus_id WHERE {w}
        UNION ALL
        SELECT a.id, a.date, COALESCE(a.trip_no,'All'), s.name, 'ABSENCE', a.reason, '', a.declared_by, a.status, ''
        FROM absences a JOIN students s ON s.id=a.student_id WHERE a.date BETWEEN ? AND ? ORDER BY 2 DESC""", p + p)


def r_batch(f):
    w, p = run_filter(f, m="m")
    return df(f"""SELECT r.trip_no AS Trip, m.batch AS Batch, m.grp AS "Group", m.purpose AS Purpose, COUNT(DISTINCT r.id) AS "Bus runs",
        COUNT(m.id) AS Planned, SUM(m.status='COMPLETED') AS Completed, SUM(m.status='RETURNED') AS Returned,
        SUM(m.status='ABSENT_DECLARED') AS Absent, SUM(m.status='NO_SHOW') AS "No show",
        SUM(m.status IN ('EXPECTED','ONBOARD','TRANSFER_PENDING')) AS Open
        FROM manifest m JOIN trip_runs r ON r.id=m.run_id WHERE {w} GROUP BY r.trip_no, m.batch, m.grp, m.purpose ORDER BY 1, 2, 3""", p)


def r_journey(f):
    sid = f.get("student")
    if not sid:
        return pd.DataFrame()
    out = df("""SELECT e.captured_ts AS Captured, e.received_ts AS Received, b.bus_no AS Bus, r.trip_no AS Trip, m.batch AS Batch,
        m.grp AS "Group", m.purpose AS Purpose, e.kind AS Action, e.result AS Result, e.reason AS Detail,
        COALESCE(st.name, CASE WHEN e.stop_id=0 THEN 'School' END) AS Stop, e.lat AS Lat, e.lon AS Lon, e.accuracy AS "GPS acc. (m)",
        e.operator AS Operator, d.name AS Device, e.method AS Method, m.handover AS Handover,
        COALESCE(m.recipient_name, m.received_by) AS "Recipient / receiver", m.status AS "Line status", e.sync_state AS Sync
        FROM events e LEFT JOIN manifest m ON m.id=e.manifest_id LEFT JOIN trip_runs r ON r.id=e.run_id LEFT JOIN buses b ON b.id=r.bus_id
        LEFT JOIN stops st ON st.id=e.stop_id LEFT JOIN devices d ON d.id=e.device_id
        WHERE e.student_id=? AND substr(e.captured_ts,1,10) BETWEEN ? AND ? ORDER BY e.captured_ts""", (sid, f["d1"], f["d2"]))
    return _status(out, "Line status")


# ------------------------------------------------------------------ registry
REPORTS = [
    # key, category, title, definition, fn, drill ('run' | 'student' | None)
    ("live", "Operations", "Live trips", "Trips currently In Progress or Pending Sync, with counts by status and the last accepted scan.", r_live, "run"),
    ("trip_summary", "Operations", "Trip manifest summary", "One row per bus run: planned children (by batch/group/purpose) and how each was accounted for.", r_trip_summary, "run"),
    ("onboard", "Operations", "On board now", "Children whose latest accepted event is Check In and who have not been checked out.", r_onboard, "student"),
    ("unaccounted", "Safety", "Unaccounted children", "Lines still Expected, On board or Awaiting transfer. A trip cannot close while any exist.", r_unaccounted, "student"),
    ("recon", "Safety", "Trip reconciliation", "Closed trips: planned vs accounted-for children, named sweep, closer. Reconciled = every line has a final status.", r_reconciliation, "run"),
    ("punctuality", "Operations", "Punctuality", "Actual trip start/end vs the scheduled window. Late = started more than 10 minutes after window start.", r_punctuality, "run"),
    ("crew", "Operations", "Crew confirmation", "Crew confirmed at trip start vs final crew recorded at closure.", r_crew, "run"),
    ("capacity", "Operations", "Capacity utilisation", "Peak on board = highest simultaneous count from check-in/check-out times, vs bus capacity.", r_capacity, "run"),
    ("duration", "Operations", "Ride duration", "Minutes between a child's check-in and check-out, by bus, trip and purpose.", r_duration, None),
    ("stops", "Operations", "Stop activity", "Per stop: pickups, home releases, no-shows, absences and children not released.", r_stops, None),
    ("attendance", "Students", "Transport attendance by student", "Planned journeys vs travelled, declared absences and undeclared no-shows per student.", r_attendance, "student"),
    ("receipt", "Handover", "School receipt log", "Children checked out at school and the receiving staff member who accepted them.", r_receipt, "student"),
    ("release", "Handover", "Home release log", "Children released at a stop and the authorized recipient they were handed to.", r_release, "student"),
    ("returned", "Safety", "Returned to school / not released", "Drop-off children brought back to school (no approved recipient, or rejected transfer).", r_returned, "student"),
    ("absnoshow", "Students", "Absences vs no-shows", "Declared absence = parent/office declared before the trip. No-show = expected but did not board, not declared.", r_abs_vs_noshow, None),
    ("incidents", "Safety", "Incidents & escalation", "All incidents with severity, assignment, resolution and number of logged contact attempts.", r_incidents, None),
    ("sweep", "Safety", "Sweep compliance", "Every closed trip must record who physically swept the bus. Minutes after last child = sweep time − last check-out.", r_sweep, "run"),
    ("scanerr", "Data quality", "Scan rejections & flags", "Events the server rejected or flagged, grouped into categories.", r_scan_errors, None),
    ("corrections", "Data quality", "Corrections", "Requested and approved changes. Original events are never altered; corrections are stored separately.", r_corrections, None),
    ("sync", "Data quality", "Offline & sync", "Events received more than 60 s after capture, sync conflicts and device clock issues.", r_sync, None),
    ("gps", "Data quality", "GPS quality", "Share of accepted scans without a GPS fix and the average reported accuracy, per bus.", r_gps, None),
    ("notifications", "Parents", "Notification delivery", "Messages by type and channel: delivered, failed, retried and delayed-offline.", r_notifications, None),
    ("badges", "Setup", "Badge changes", "Revoked / replaced badges and any scan attempts made with a revoked badge.", r_badges, None),
    ("audit", "Setup", "Audit log", "Who did what and when (logins, trips, approvals, setup changes). Latest 5,000 entries.", r_audit, None),
    ("wrong", "Safety", "Wrong bus / wrong stop", "Children who boarded a bus they are not assigned to, or were scanned at the wrong stop.", r_wrong, None),
    ("manual", "Data quality", "Manual entries", "Scans entered by hand: reason and second-person verification.", r_manual, None),
    ("overcap", "Safety", "Overcapacity", "Runs with peak load ≥ 90 % of capacity or a recorded overcapacity override.", r_overcap, "run"),
    ("changes", "Parents", "Change requests & absences", "Parent/office requests for a different stop, bus or one-day recipient, plus declared absences.", r_changes, None),
    ("batch", "Students", "Batch / group summary", "Totals by trip, batch, group and purpose (Trip 2 shows its three movements separately).", r_batch, None),
    ("journey", "Students", "Student journey record", "Every event for one student: times, coordinates, accuracy, operator, device, method, handover and sync state.", r_journey, None),
]
BY_KEY = {r[0]: r for r in REPORTS}


def freshness():
    r = db.q1("SELECT MAX(received_ts) t FROM events")
    p = db.q1("SELECT COUNT(*) n FROM trip_runs WHERE status='PENDING_SYNC'")
    return (r["t"] or "no data"), p["n"]


# ------------------------------------------------------------------ exports
def to_pdf(title, definition, filters_text, frame, max_rows=600):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm,
                            title=title)
    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=7, leading=8.5)
    fresh, pend = freshness()
    story = [Paragraph(f"{db.SCHOOL_NAME} — {title}", ss["Title"]),
             Paragraph(f"<b>Definition:</b> {definition}", ss["BodyText"]),
             Paragraph(f"<b>Filters:</b> {filters_text} &nbsp; <b>Generated:</b> {db.ts()} &nbsp; <b>Data as of:</b> {fresh}"
                       f" &nbsp; <b>Trips pending sync:</b> {pend}", ss["BodyText"]), Spacer(1, 4 * mm)]
    if frame.empty:
        story.append(Paragraph("No data for these filters.", ss["BodyText"]))
    else:
        f2 = frame.head(max_rows).fillna("")
        data = [[Paragraph(f"<b>{c}</b>", small) for c in f2.columns]] + [[Paragraph(str(v), small) for v in row] for row in f2.values.tolist()]
        width = landscape(A4)[0] - 20 * mm
        t = Table(data, repeatRows=1, colWidths=[width / len(f2.columns)] * len(f2.columns))
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1c5490")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")])]))
        story.append(t)
        if len(frame) > max_rows:
            story.append(Paragraph(f"Showing first {max_rows} of {len(frame)} rows — use CSV for the full data.", small))
    doc.build(story)
    return buf.getvalue()


def roster_pdf(run_or_bus_lines, title):
    """Printable contingency roster with tick boxes (spec §6)."""
    frame = pd.DataFrame([{"#": i + 1, "Student": ln["name"], "ID": ln["code"], "Class": ln["class"], "Batch/Group": f"{ln['batch']} {ln['grp']}",
                           "Purpose": ln["purpose"].title(), "Stop": ln.get("stop_name") or "", "Status": STATUS_TEXT.get(ln["status"], ln["status"]),
                           "IN ☐ time": "", "OUT ☐ time": "", "Handed to / received by": ""} for i, ln in enumerate(run_or_bus_lines)])
    return to_pdf(title, "Paper fallback if the device fails. Tick IN/OUT, write the time and the recipient; enter into the system as manual "
                  "entries (with reason 'Paper roster') once the device is back.", "—", frame)
