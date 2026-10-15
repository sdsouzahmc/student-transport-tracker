"""Business rules: manifests, scans (check-in / check-out), handover, closure, exceptions, offline sync, notifications, simulator."""
import random
import uuid
from datetime import datetime, timedelta

import db
from db import SCHOOL_STOP, TRIPS, q, q1, x, ts

ACTIVE_LINE = ("EXPECTED", "ONBOARD", "TRANSFER_PENDING")
FINAL_LINE = ("COMPLETED", "RETURNED", "ABSENT_DECLARED", "NO_SHOW", "TRANSFERRED_OUT", "CANCELLED")
STATUS_LABEL = {
    "EXPECTED": "Expected", "ONBOARD": "On board", "COMPLETED": "Completed", "RETURNED": "Returned to school",
    "ABSENT_DECLARED": "Declared absent", "NO_SHOW": "No show", "TRANSFER_PENDING": "Awaiting transfer approval",
    "TRANSFERRED_OUT": "Transferred to other bus", "CANCELLED": "Transport cancelled",
}


# ------------------------------------------------------------------ trips & windows
def parse_hm(d, hm):
    return datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M")


def trip_for_time(dt=None):
    dt = dt or db.now()
    d = dt.strftime("%Y-%m-%d")
    for no, t in TRIPS.items():
        a, b = parse_hm(d, t["window"][0]), parse_hm(d, t["window"][1])
        if a - timedelta(minutes=30) <= dt <= b:
            return no
    # nearest upcoming trip, else last
    for no, t in TRIPS.items():
        if dt < parse_hm(d, t["window"][0]):
            return no
    return 5


def in_window(trip_no, dt):
    d = dt.strftime("%Y-%m-%d")
    a, b = TRIPS[trip_no]["window"]
    return parse_hm(d, a) - timedelta(minutes=15) <= dt <= parse_hm(d, b)


def trips_for(batch, grp):
    """Which of the five trips a batch/group rides, e.g. 'Trip 1 pickup, Trip 2 drop-off'."""
    out = []
    for no, t in TRIPS.items():
        for b, g, p in t["movements"]:
            if b == batch and g == grp:
                out.append(f"Trip {no} {'pickup' if p == 'PICKUP' else 'drop-off'}")
    return ", ".join(out)


def _discard_plans(c, run_ids):
    """v2.0: route plans of removed trips stay in the history but are no longer active (the trip gets a fresh plan when restarted)."""
    import planner
    planner.ensure_schema(c)
    c.executemany("UPDATE route_plans SET status='DISCARDED', superseded_ts=? WHERE run_id=? AND status IN ('ACTIVE','DRAFT')",
                  [(ts(), i) for i in run_ids])


def cancel_run(run_id, user, role, reason="Started by mistake", con=None):
    """Remove a trip that was started by mistake. Only allowed while nothing has been scanned on it."""
    with db.tx(con) as c:
        run = get_run(run_id, c)
        if not run or run["status"] not in ("IN_PROGRESS", "PENDING_SYNC"):
            return False, "Trip is not open."
        if q1("SELECT id FROM events WHERE run_id=? AND result<>'REJECTED' AND kind IN ('CHECK_IN','CHECK_OUT') LIMIT 1", (run_id,), c):
            return False, "A child has already been checked in on this trip — it cannot be cancelled. Close it normally."
        c.execute("DELETE FROM notifications WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM incidents WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM manifest WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM trip_runs WHERE id=?", (run_id,))
        _discard_plans(c, [run_id])
        db.audit(user, role, "TRIP_CANCELLED", "trip_run", run_id, f"bus {run['bus_id']} trip {run['trip_no']} {run['date']}: {reason}", c)
        return True, f"Trip {run['trip_no']} cancelled. You can now start the correct trip."


def reset_scope(date, bus_id=None, trip_no=None, con=None):
    """Trips that a reset for this date / bus / trip would remove, with counts (for the confirmation preview)."""
    sql = """SELECT r.id, b.bus_no, r.trip_no, r.status, r.start_ts, r.end_ts,
                    (SELECT COUNT(*) FROM manifest m WHERE m.run_id=r.id) children,
                    (SELECT COUNT(*) FROM manifest m WHERE m.run_id=r.id AND m.status IN ('ONBOARD','TRANSFER_PENDING')) onboard,
                    (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id) scans,
                    (SELECT COUNT(*) FROM notifications n WHERE n.run_id=r.id) notices,
                    (SELECT COUNT(*) FROM incidents i WHERE i.run_id=r.id) incidents
             FROM trip_runs r JOIN buses b ON b.id=r.bus_id WHERE r.date=?"""
    p = [date]
    if bus_id:
        sql += " AND r.bus_id=?"; p.append(bus_id)
    if trip_no:
        sql += " AND r.trip_no=?"; p.append(trip_no)
    return q(sql + " ORDER BY b.bus_no, r.trip_no", p, con)


def parent_changes_scope(date, bus_id=None, trip_no=None, con=None):
    """Absences and change requests for this date (optionally for one bus's children / one trip)."""
    out = {}
    for tbl in ("absences", "change_requests"):
        sql = f"SELECT x.id FROM {tbl} x JOIN students s ON s.id=x.student_id WHERE x.date=?"
        p = [date]
        if bus_id:
            sql += " AND s.bus_id=?"; p.append(bus_id)
        if trip_no:
            sql += " AND x.trip_no=?"; p.append(trip_no)
        # closed trips are final: keep absences / requests that belong to a closed trip of the child's bus
        sql += """ AND NOT EXISTS (SELECT 1 FROM trip_runs r WHERE r.date=x.date AND r.bus_id=s.bus_id AND r.status='CLOSED'
                                   AND (x.trip_no IS NULL OR r.trip_no=x.trip_no))"""
        out[tbl] = [r["id"] for r in q(sql, p, con)]
    return out


def reset_trips(date, user, role, bus_id=None, trip_no=None, parent_changes=False, include_closed=False):
    """Admin tool: permanently remove trips for a date (optionally one bus and/or one trip) with their manifest, scans,
    parent notices and incidents, so the trip(s) can be run again. The reset itself is written to the audit log."""
    if not date:
        raise ValueError("Date is required.")
    with db.tx() as c:
        runs = reset_scope(date, bus_id, trip_no, c)
        # a closed trip is final; only the administrator's explicit testing override removes it
        kept = [] if include_closed else [r for r in runs if r["status"] == "CLOSED"]
        ids = [r["id"] for r in runs if include_closed or r["status"] != "CLOSED"]
        for t_ in ("manifest", "events", "notifications", "incidents"):
            c.executemany(f"DELETE FROM {t_} WHERE run_id=?", [(i,) for i in ids])
        c.executemany("DELETE FROM trip_runs WHERE id=?", [(i,) for i in ids])
        _discard_plans(c, ids)
        removed_pc = 0
        if parent_changes:
            pc = parent_changes_scope(date, bus_id, trip_no, c)
            for tbl, rows in pc.items():
                c.executemany(f"DELETE FROM {tbl} WHERE id=?", [(i,) for i in rows])
                removed_pc += len(rows)
        bus_no = q1("SELECT bus_no FROM buses WHERE id=?", (bus_id,), c)["bus_no"] if bus_id else "all buses"
        scope = f"{date} · {bus_no} · {'Trip ' + str(trip_no) if trip_no else 'all trips'}"
        db.audit(user, role, "TRIP_DATA_RESET_FORCED" if include_closed else "TRIP_DATA_RESET", "trip_runs", date,
                 f"{scope}: {len(ids)} trip(s) removed, {len(kept)} closed trip(s) kept" + (f", {removed_pc} absence/change request(s) removed" if parent_changes else ""), c)
        return len(ids), removed_pc, len(kept)


def reset_open_trips_any_date(bus_id, user, role):
    """Remove trips of one bus that were left open on earlier days (e.g. the dataset's unclosed custody case)."""
    days = [r["date"] for r in q("SELECT DISTINCT date FROM trip_runs WHERE bus_id=? AND status IN ('IN_PROGRESS','PENDING_SYNC','PLANNED')",
                                 (bus_id,))]
    return sum(reset_trips(d_, user, role, bus_id=bus_id)[0] for d_ in days)


def reset_day(date, user, role):
    """Remove all open trips for one date (used by Prepare demo scenarios). Closed trips are kept."""
    n, _, kept = reset_trips(date, user, role)
    return n, kept


def link_guardian(student_id, guardian_id, user, role, as_recipient=True, con=None):
    """Link an existing guardian to another child (siblings share a parent) and make them an authorized recipient."""
    with db.tx(con) as c:
        g = q1("SELECT * FROM guardians WHERE id=?", (guardian_id,), c)
        c.execute("INSERT OR IGNORE INTO student_guardians VALUES (?,?,0)", (student_id, guardian_id))
        if as_recipient and not q1("SELECT id FROM recipients WHERE student_id=? AND name=? AND active=1", (student_id, g["name"]), c):
            c.execute("INSERT INTO recipients(student_id, name, relation, phone, id_number) VALUES (?,?,?,?,?)",
                      (student_id, g["name"], g["relation"] or "Guardian", g["phone"] or "", "sibling link"))
        db.audit(user, role, "GUARDIAN_LINKED", "student", student_id, f"{g['name']} linked (siblings)", c)


def prepare_demo(user, role):
    """Demo tool: clean today and set up the data the 10 normal scenarios need (siblings, relief crew)."""
    removed, kept = reset_day(db.today(), user, role)
    out = [f"Today's open trips cleared ({removed})." + (f" {kept} closed trip(s) kept — closed trips cannot be reset." if kept else "")]
    with db.tx() as c:
        for name, r in (("Relief Driver", "driver"), ("Relief Supervisor", "supervisor"), ("Relief Care-taker", "caretaker")):
            if not q1("SELECT id FROM staff WHERE name=?", (name,), c):
                c.execute("INSERT INTO staff(name, role, phone) VALUES (?,?,?)", (name, r, "+974 5000 0000"))
        out.append("Relief Driver / Supervisor / Care-taker available.")
        for b in q("SELECT * FROM buses", (), c):      # restore each bus's normal crew (scenario 10 changes it)
            n = int(b["bus_no"][-3:]) if b["bus_no"][-3:].isdigit() else 0
            for col, nm in (("driver_id", f"Driver {n}"), ("supervisor_id", f"Supervisor {n}"), ("caretaker_id", f"Care-taker {n}")):
                st_ = q1("SELECT id FROM staff WHERE name=?", (nm,), c)
                if st_:
                    c.execute(f"UPDATE buses SET {col}=? WHERE id=?", (st_["id"], b["id"]))
        c.execute("DELETE FROM absences WHERE date>=?", (db.today(),))
        c.execute("DELETE FROM change_requests WHERE date>=?", (db.today(),))
        a = q1("SELECT id FROM students WHERE code='ST051'", (), c)
        bsib = q1("SELECT id FROM students WHERE code='ST054'", (), c)
        g = q1("SELECT g.id FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id WHERE sg.student_id=? AND sg.is_primary=1", (a["id"],), c) if a else None
        if a and bsib and g:
            link_guardian(bsib["id"], g["id"], user, role, con=c)
            out.append("Siblings: Student 051 and Student 054 (Route 2 Stop 2) now share Guardian 051.")
        db.audit(user, role, "DEMO_PREPARED", "demo", db.today(), " ".join(out), c)
    return out


# ------------------------------------------------------------------ manifests
def effective_assignment(student, date, trip_no, con):
    """Bus / stop for a student on a date+trip after approved change requests."""
    bus_id, stop_id = student["bus_id"], student["stop_id"]
    for cr in q("""SELECT * FROM change_requests WHERE student_id=? AND date=? AND status='APPROVED' AND kind IN ('BUS','STOP')
                   AND (trip_no IS NULL OR trip_no=?) ORDER BY id""", (student["id"], date, trip_no), con):
        if cr["kind"] == "BUS" and cr["new_bus_id"]:
            bus_id = cr["new_bus_id"]
            stop_id = cr["new_stop_id"] or stop_id
        elif cr["kind"] == "STOP" and cr["new_stop_id"]:
            stop_id = cr["new_stop_id"]
    return bus_id, stop_id


def is_absent(student_id, date, trip_no, con):
    return q1("""SELECT id FROM absences WHERE student_id=? AND date=? AND status='APPROVED' AND (trip_no IS NULL OR trip_no=?)""",
              (student_id, date, trip_no), con) is not None


def manifest_for(bus_id, date, trip_no, con=None):
    """Planned manifest (before a run exists): list of dicts with student, purpose, stop, planned status."""
    with db.tx(con) as c:
        out = []
        for batch, grp, purpose in TRIPS[trip_no]["movements"]:
            for s in q("""SELECT * FROM students WHERE active=1 AND batch=? AND grp=? AND (bus_id=? OR id IN
                          (SELECT student_id FROM change_requests WHERE date=? AND status='APPROVED' AND kind='BUS'))""",
                       (batch, grp, bus_id, date), c):
                b, st = effective_assignment(s, date, trip_no, c)
                if b != bus_id:
                    continue
                if purpose == "DROPOFF" and st == s["stop_id"] and s.get("drop_stop_id"):
                    st = s["drop_stop_id"]          # v2.0: the student's own drop-off stop (no one-day change approved)
                out.append({"student_id": s["id"], "code": s["code"], "name": s["name"], "class": f"{s['class_name']}{s['section']}",
                            "batch": batch, "grp": grp, "purpose": purpose, "stop_id": st,
                            "status": "ABSENT_DECLARED" if is_absent(s["id"], date, trip_no, c) else "EXPECTED"})
        return out


def run_lines(run_id, con=None):
    return q("""SELECT m.*, s.code, s.name, s.class_name || s.section AS class, st.name AS stop_name, st.seq AS stop_seq
                FROM manifest m JOIN students s ON s.id=m.student_id LEFT JOIN stops st ON st.id=m.stop_id
                WHERE m.run_id=? ORDER BY m.purpose DESC, st.seq, s.name""", (run_id,), con)


def get_run(run_id, con=None):
    return q1("SELECT * FROM trip_runs WHERE id=?", (run_id,), con)


def open_run(bus_id, con=None):
    return q1("SELECT * FROM trip_runs WHERE bus_id=? AND status IN ('IN_PROGRESS','PENDING_SYNC') ORDER BY id DESC LIMIT 1", (bus_id,), con)


def start_run(bus_id, date, trip_no, driver_id, supervisor_id, caretaker_id, lat, lon, user, role, device_id, when=None, con=None):
    with db.tx(con) as c:
        dev = q1("SELECT * FROM devices WHERE id=?", (device_id,), c) if device_id else None
        if device_id and (not dev or dev["status"] != "ACTIVE"):
            return None, "This device has been revoked by the transport office. Scanning is not allowed."
        if not (driver_id and supervisor_id and caretaker_id):
            return None, "Confirm the driver, supervisor and care-taker before starting."
        existing = q1("SELECT * FROM trip_runs WHERE bus_id=? AND date=? AND trip_no=?", (bus_id, date, trip_no), c)
        if existing and existing["status"] != "PLANNED":
            return None, f"Trip {trip_no} for this bus on {date} has already been started ({existing['status']})."
        when = when or db.now()
        if existing:
            run_id = existing["id"]
            c.execute("""UPDATE trip_runs SET status='IN_PROGRESS', start_ts=?, start_lat=?, start_lon=?, driver_id=?, supervisor_id=?,
                         caretaker_id=?, started_by=?, device_id=? WHERE id=?""",
                      (ts(when), lat, lon, driver_id, supervisor_id, caretaker_id, user, device_id, run_id))
        else:
            run_id = c.execute("""INSERT INTO trip_runs(bus_id, date, trip_no, status, start_ts, start_lat, start_lon, driver_id, supervisor_id,
                                  caretaker_id, started_by, device_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (bus_id, date, trip_no, "IN_PROGRESS", ts(when), lat, lon, driver_id, supervisor_id, caretaker_id, user,
                                device_id)).lastrowid
        for ln in manifest_for(bus_id, date, trip_no, c):
            c.execute("""INSERT OR IGNORE INTO manifest(run_id, student_id, purpose, batch, grp, stop_id, status) VALUES (?,?,?,?,?,?,?)""",
                      (run_id, ln["student_id"], ln["purpose"], ln["batch"], ln["grp"], ln["stop_id"], ln["status"]))
        db.audit(user, role, "TRIP_START", "trip_run", run_id, f"bus {bus_id} trip {trip_no} {date}", c)
        return run_id, "Trip started."


# ------------------------------------------------------------------ notifications
def notify(student_id, run_id, kind, message, delayed=False, con=None, rnd=random, at=None):
    with db.tx(con) as c:
        for g in q("""SELECT g.* FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id
                      WHERE sg.student_id=? AND g.verified=1""", (student_id,), c):
            for ch in ("IN_APP", "EMAIL"):
                ok = ch == "IN_APP" or rnd.random() > 0.06     # simulate occasional email delivery failure
                c.execute("""INSERT INTO notifications(ts, guardian_id, student_id, run_id, channel, kind, message, status, attempts, last_error,
                             delayed_offline, sent_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (ts(at), g["id"], student_id, run_id, ch, kind, message, "SENT" if ok else "FAILED", 1,
                           None if ok else "Mailbox temporarily unavailable (simulated)", 1 if delayed else 0, ts(at) if ok else None))


def retry_failed(max_attempts=3, con=None):
    with db.tx(con) as c:
        rows = q("SELECT id, attempts FROM notifications WHERE status='FAILED' AND attempts < ?", (max_attempts,), c)
        sent = 0
        for r in rows:
            ok = random.random() > 0.3
            c.execute("UPDATE notifications SET status=?, attempts=attempts+1, last_error=?, sent_ts=? WHERE id=?",
                      ("SENT" if ok else "FAILED", None if ok else "Retry failed (simulated)", ts() if ok else None, r["id"]))
            sent += ok
        return len(rows), sent


def broadcast_run(run_id, kind, text, user, role, con=None):
    """Delay / cancellation notice to every guardian on a trip's manifest."""
    with db.tx(con) as c:
        run = get_run(run_id, c)
        b = db.bus(run["bus_id"], c)
        lines = q("SELECT student_id FROM manifest WHERE run_id=? AND status NOT IN ('ABSENT_DECLARED')", (run_id,), c)
        for ln in lines:
            notify(ln["student_id"], run_id, kind, f"{b['bus_no']} Trip {run['trip_no']}: {text}", con=c)
        if kind == "CANCELLED":
            c.execute("UPDATE trip_runs SET cancelled=1 WHERE id=?", (run_id,))
        db.audit(user, role, f"NOTICE_{kind}", "trip_run", run_id, text, c)
        return len(lines)


# ------------------------------------------------------------------ incidents
def open_incident(kind, severity, details, run_id=None, bus_id=None, student_id=None, con=None, when=None):
    return x("""INSERT INTO incidents(ts, run_id, bus_id, student_id, kind, severity, details, status) VALUES (?,?,?,?,?,?,?,?)""",
             (ts(when), run_id, bus_id, student_id, kind, severity, details, "OPEN"), con)


# ------------------------------------------------------------------ office <-> bus: messages and SOS
SOS_TYPES = ["Medical", "Accident", "Breakdown", "Security / threat", "Child missing", "Other"]


def _ensure_comms(con=None):
    x("""CREATE TABLE IF NOT EXISTS bus_messages (id INTEGER PRIMARY KEY, ts TEXT, bus_id INTEGER, run_id INTEGER, sender TEXT,
         text TEXT, ack_ts TEXT, ack_by TEXT)""", (), con)


def send_bus_message(bus_id, run_id, text, sender, role):
    """Office -> bus message; shown on the crew and driver screens until a crew member acknowledges it."""
    with db.tx() as c:
        _ensure_comms(c)
        mid = x("INSERT INTO bus_messages(ts, bus_id, run_id, sender, text) VALUES (?,?,?,?,?)", (ts(), bus_id, run_id, sender, text), c)
        db.audit(sender, role, "BUS_MESSAGE", "bus", bus_id, text, c)
        return mid


def bus_messages(bus_id, only_open=False, limit=20):
    _ensure_comms()
    return q("SELECT * FROM bus_messages WHERE bus_id=?" + (" AND ack_ts IS NULL" if only_open else "") + " ORDER BY id DESC LIMIT ?",
             (bus_id, limit))


def ack_bus_message(msg_id, username, role):
    with db.tx() as c:
        _ensure_comms(c)
        c.execute("UPDATE bus_messages SET ack_ts=?, ack_by=? WHERE id=? AND ack_ts IS NULL", (ts(), username, msg_id))
        db.audit(username, role, "BUS_MESSAGE_ACK", "bus_message", msg_id, "", c)


def raise_sos(bus_id, run_id, sos_type, note, username, role, lat=None, lon=None):
    """Crew emergency: opens a CRITICAL incident (top of Live trips, Overview and Incidents) with the bus position."""
    b = db.bus(bus_id)
    where = f" · GPS {lat:.5f}, {lon:.5f}" if lat is not None else " · GPS unavailable"
    with db.tx() as c:
        iid = open_incident("SOS", "CRITICAL", f"SOS {sos_type} on {b['bus_no']} raised by {username}" + (f": {note}" if note else "") + where,
                            run_id=run_id, bus_id=bus_id, con=c)
        db.audit(username, role, "SOS", "incident", iid, f"{b['bus_no']} {sos_type} {note}", c)
        return iid


def open_sos(bus_id):
    return q("SELECT * FROM incidents WHERE bus_id=? AND kind='SOS' AND status<>'RESOLVED' ORDER BY id DESC", (bus_id,))


# ------------------------------------------------------------------ scans
def allowed_recipients(student_id, date, trip_no, con=None):
    rec = [{"name": r["name"], "relation": r["relation"], "phone": r["phone"], "source": "Authorized"}
           for r in q("SELECT * FROM recipients WHERE student_id=? AND active=1", (student_id,), con)]
    for cr in q("""SELECT * FROM change_requests WHERE student_id=? AND date=? AND kind='RECIPIENT' AND status='APPROVED'
                   AND (trip_no IS NULL OR trip_no=?)""", (student_id, date, trip_no), con):
        rec.append({"name": cr["recipient_name"], "relation": "One-day (approved request)", "phone": cr["recipient_phone"], "source": "Approved change"})
    return rec


def _event(c, run, ctx, kind, result, reason, student_id=None, manifest_id=None, badge_code=None):
    rec = ctx.get("received_ts") or ts()
    cap = ctx.get("captured_ts") or rec
    flag = None
    try:
        dcap = datetime.strptime(cap, "%Y-%m-%d %H:%M:%S")
        drec = datetime.strptime(rec, "%Y-%m-%d %H:%M:%S")
        if dcap > drec + timedelta(minutes=2):
            flag = "Device clock ahead of server"
        elif (drec - dcap) > timedelta(hours=12):
            flag = "Very old capture time"
    except ValueError:
        flag = "Unreadable capture time"
    c.execute("""INSERT INTO events(client_uuid, run_id, manifest_id, student_id, badge_code, kind, result, reason, captured_ts, received_ts,
                 lat, lon, accuracy, gps_ok, stop_id, operator, device_id, method, manual_reason, verified_by, sync_state, clock_flag, connectivity)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ctx.get("client_uuid") or str(uuid.uuid4()), run["id"] if run else None, manifest_id, student_id, badge_code, kind, result,
               reason, cap, rec, ctx.get("lat"), ctx.get("lon"), ctx.get("accuracy"),
               1 if ctx.get("gps_ok", True) else 0, ctx.get("stop_id"), ctx.get("operator"), ctx.get("device_id"), ctx.get("method", "QR"),
               ctx.get("manual_reason"), ctx.get("verified_by"), ctx.get("sync_state", "SYNCED"), flag,
               "Offline" if ctx.get("offline") else "Online"))


def process(run_id, kind, ctx, badge_code=None, student_id=None, con=None, rnd=random):
    """Validate and apply one crew action. kind: CHECK_IN | CHECK_OUT | NO_SHOW.
    Returns dict(ok, level, message, line, needs) where level is ok|warn|error and needs may ask the UI for a handover."""
    with db.tx(con) as c:
        if ctx.get("client_uuid") and q1("SELECT id FROM events WHERE client_uuid=?", (ctx["client_uuid"],), c):
            return {"ok": True, "level": "ok", "message": "Already received (duplicate sync ignored).", "duplicate": True}
        run = get_run(run_id, c)
        delayed = bool(ctx.get("offline"))

        def reject(reason, sid=None, mid=None):
            _event(c, run, ctx, kind, "REJECTED", reason, sid, mid, badge_code)
            return {"ok": False, "level": "error", "message": reason}

        if not run or run["status"] not in ("IN_PROGRESS", "PENDING_SYNC"):
            return reject("Trip is not in progress.")
        dev = q1("SELECT status FROM devices WHERE id=?", (ctx.get("device_id"),), c) if ctx.get("device_id") else {"status": "ACTIVE"}
        if not dev or dev["status"] != "ACTIVE":
            return reject("Device revoked — scans from this device are not accepted.")
        if ctx.get("role") == "driver":
            return reject("Drivers must not scan. The supervisor or care-taker scans students.")
        if ctx.get("method") == "MANUAL" and not (ctx.get("manual_reason") and ctx.get("verified_by")):
            return reject("Manual entry needs a reason and a second person who verified the child.")

        # identify the student
        if badge_code is not None:
            badge = q1("SELECT * FROM badges WHERE code=?", (badge_code.strip().upper(),), c)
            if not badge:
                code_ = badge_code.strip().upper()
                st_ = q1("SELECT name FROM students WHERE UPPER(code)=? OR UPPER(code)=?", (code_, "ST" + code_.zfill(3)), c)
                if st_:
                    return reject(f"'{badge_code}' is the student ID of {st_['name']}, not a badge code. Scan the badge, or use Manual entry "
                                  "(reason + second crew member) if the badge is not available.")
                return reject(f"Invalid badge: {badge_code}")
            if badge["status"] != "ACTIVE":
                return reject(f"Badge {badge_code} was revoked ({badge['reason'] or 'replaced'}). Use the student's new badge.", badge["student_id"])
            student_id = badge["student_id"]
        stu = q1("SELECT * FROM students WHERE id=?", (student_id,), c)
        if not stu:
            return reject("Unknown student.")
        b = db.bus(run["bus_id"], c)
        line = q1("SELECT * FROM manifest WHERE run_id=? AND student_id=?", (run_id, student_id), c)
        cur_stop = ctx.get("stop_id")
        tlabel = f"{b['bus_no']} Trip {run['trip_no']}"
        when = ctx.get("captured_ts") or ts()
        at = ctx.get("received_ts") or when
        hm = when[11:16]
        flags = []

        # ---------------------------------------------------- NO SHOW
        if kind == "NO_SHOW":
            if not line or line["status"] != "EXPECTED":
                return reject(f"{stu['name']}: only an expected student can be marked no-show.", student_id, line and line["id"])
            c.execute("UPDATE manifest SET status='NO_SHOW', note=? WHERE id=?", (ctx.get("note") or "", line["id"]))
            _event(c, run, ctx, kind, "ACCEPTED", "No show", student_id, line["id"], badge_code)
            if line["purpose"] == "DROPOFF":
                open_incident("MISSING_STUDENT", "HIGH", f"{stu['name']} ({stu['code']}) did not board {tlabel} at school — locate the child.",
                              run_id, run["bus_id"], student_id, c, at)
                notify(student_id, run_id, "NO_SHOW", f"{stu['name']} did not board {tlabel} at school. The school is checking — "
                       f"call {db.TRANSPORT_PHONE} if urgent.", delayed, c, rnd, at)
            else:
                notify(student_id, run_id, "NO_SHOW", f"{stu['name']} was not at the stop for {tlabel} at {hm}.", delayed, c, rnd, at)
            return {"ok": True, "level": "warn", "message": f"No show recorded: {stu['name']}"}

        # ---------------------------------------------------- CHECK IN
        if kind == "CHECK_IN":
            if not line:
                other = q1("""SELECT b.bus_no FROM manifest m JOIN trip_runs r ON r.id=m.run_id JOIN buses b ON b.id=r.bus_id
                              WHERE m.student_id=? AND r.date=? AND r.trip_no=?""", (student_id, run["date"], run["trip_no"]), c)
                planned = manifest_for_student(stu, run["date"], run["trip_no"], c)
                if not planned:
                    return reject(f"NOT ON THIS TRIP: {stu['name']} is Batch {stu['batch']} {stu['grp']} and rides "
                                  f"{trips_for(stu['batch'], stu['grp'])} — this is Trip {run['trip_no']} "
                                  f"({TRIPS[run['trip_no']]['label']}).", student_id)
                lid = c.execute("""INSERT INTO manifest(run_id, student_id, purpose, batch, grp, stop_id, status, entry_ts, entry_lat, entry_lon, note)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                (run_id, student_id, planned["purpose"], stu["batch"], stu["grp"], planned["stop_id"], "TRANSFER_PENDING",
                                 when, ctx.get("lat"), ctx.get("lon"), f"Assigned to {planned['bus_no']}")).lastrowid
                _event(c, run, ctx, kind, "FLAGGED", f"Wrong bus: assigned to {planned['bus_no']}", student_id, lid, badge_code)
                open_incident("WRONG_BUS", "MEDIUM", f"{stu['name']} ({stu['code']}) boarded {b['bus_no']} but is assigned to {planned['bus_no']}"
                              f"{' (also on that bus manifest)' if other else ''}. Approve the transfer or correct.", run_id, run["bus_id"], student_id, c, at)
                return {"ok": True, "level": "warn", "message": f"WRONG BUS: {stu['name']} belongs to {planned['bus_no']}. "
                        "Kept as 'awaiting transfer approval' — the trip cannot close until the office decides."}
            if line["status"] in ("ONBOARD", "TRANSFER_PENDING"):
                return reject(f"{stu['name']} is already checked in (duplicate).", student_id, line["id"])
            if line["status"] in ("COMPLETED", "RETURNED"):
                return reject(f"{stu['name']} has already completed this trip.", student_id, line["id"])
            onboard_n = q1("SELECT COUNT(*) n FROM manifest WHERE run_id=? AND status IN ('ONBOARD','TRANSFER_PENDING')", (run_id,), c)["n"]
            if onboard_n >= b["capacity"] and not ctx.get("capacity_override"):
                return reject(f"Bus is full ({onboard_n}/{b['capacity']}). Over-capacity needs a supervisor override with a reason.", student_id, line["id"])
            if onboard_n >= b["capacity"]:
                flags.append(f"Over capacity override: {ctx['capacity_override']}")
                open_incident("OVERCAPACITY", "MEDIUM", f"{b['bus_no']} over capacity ({onboard_n + 1}/{b['capacity']}): {ctx['capacity_override']}",
                              run_id, run["bus_id"], student_id, c, at)
            expected_stop = line["stop_id"] if line["purpose"] == "PICKUP" else SCHOOL_STOP
            if cur_stop is not None and cur_stop != expected_stop:
                flags.append("Wrong stop")
                st_name = db.stop(cur_stop, c)["name"] if db.stop(cur_stop, c) else "?"
                open_incident("WRONG_STOP", "LOW", f"{stu['name']} checked in at {st_name}, expected {db.stop(expected_stop, c)['name']}.",
                              run_id, run["bus_id"], student_id, c, at)
            if line["status"] == "ABSENT_DECLARED":
                flags.append("Declared absent but boarded")
                open_incident("ABSENCE_MISMATCH", "LOW", f"{stu['name']} was declared absent but boarded {tlabel}.", run_id, run["bus_id"], student_id, c, at)
            if line["status"] == "NO_SHOW":
                flags.append("Previously marked no-show")
            c.execute("UPDATE manifest SET status='ONBOARD', entry_ts=?, entry_lat=?, entry_lon=? WHERE id=?",
                      (when, ctx.get("lat"), ctx.get("lon"), line["id"]))
            _event(c, run, ctx, kind, "FLAGGED" if flags else "ACCEPTED", "; ".join(flags) or None, student_id, line["id"], badge_code)
            where = "at school" if line["purpose"] == "DROPOFF" else f"at {db.stop(line['stop_id'], c)['name']}"
            notify(student_id, run_id, "BOARDED", f"{stu['name']} boarded {tlabel} {where} at {hm}.", delayed, c, rnd, at)
            return {"ok": True, "level": "warn" if flags else "ok",
                    "message": f"CHECKED IN: {stu['name']} ({stu['class_name']}{stu['section']}, Batch {stu['batch']} {stu['grp']}, "
                               f"{'pickup' if line['purpose'] == 'PICKUP' else 'drop-off'})" + (f" — ⚠ {'; '.join(flags)}" if flags else "")}

        # ---------------------------------------------------- CHECK OUT
        if kind == "CHECK_OUT":
            if not line or line["status"] in ("EXPECTED", "NO_SHOW", "ABSENT_DECLARED"):
                return reject(f"{stu['name']} was never checked in on this trip — check-out refused (exit without entry).",
                              student_id, line and line["id"])
            if line["status"] in ("COMPLETED", "RETURNED"):
                return reject(f"{stu['name']} is already checked out (duplicate).", student_id, line["id"])
            if line["status"] == "TRANSFER_PENDING":
                return reject(f"{stu['name']} is awaiting transfer approval by the office — do not release yet.", student_id, line["id"])
            at_school = cur_stop == SCHOOL_STOP
            if line["purpose"] == "PICKUP" or at_school:
                # arrival at school (or a drop-off child brought back to school) -> school receipt
                if not at_school:
                    return reject(f"{stu['name']} is a pickup — check out only at school with receiving staff.", student_id, line["id"])
                rec_by = ctx.get("received_by")
                if not rec_by:
                    return {"ok": False, "level": "warn", "message": f"{stu['name']}: select the receiving staff member and confirm.", "needs": "receiver",
                            "student_id": student_id, "student_name": stu["name"]}
                new_status = "COMPLETED" if line["purpose"] == "PICKUP" else "RETURNED"
                handover = "SCHOOL_RECEIPT" if line["purpose"] == "PICKUP" else "RETURNED_TO_SCHOOL"
                c.execute("""UPDATE manifest SET status=?, exit_ts=?, exit_lat=?, exit_lon=?, handover=?, received_by=? WHERE id=?""",
                          (new_status, when, ctx.get("lat"), ctx.get("lon"), handover, rec_by, line["id"]))
                _event(c, run, ctx, kind, "ACCEPTED", handover, student_id, line["id"], badge_code)
                if new_status == "COMPLETED":
                    notify(student_id, run_id, "ARRIVED", f"{stu['name']} arrived at school on {tlabel} at {hm} (received by {rec_by}).", delayed, c, rnd, at)
                    return {"ok": True, "level": "ok", "message": f"CHECKED OUT at school: {stu['name']} — received by {rec_by}"}
                c.execute("""UPDATE incidents SET receiving_custody=?, status=CASE WHEN status='OPEN' THEN 'ASSIGNED' ELSE status END
                             WHERE run_id=? AND student_id=? AND kind='NO_RECIPIENT'""", (rec_by, run_id, student_id))
                notify(student_id, run_id, "RETURNED", f"{stu['name']} was brought back to school on {tlabel} at {hm} and is with {rec_by}. "
                       f"Please call {db.TRANSPORT_PHONE}.", delayed, c, rnd, at)
                return {"ok": True, "level": "warn", "message": f"RETURNED TO SCHOOL: {stu['name']} — custody with {rec_by}"}
            # home release (drop-off)
            if cur_stop is not None and cur_stop != line["stop_id"]:
                flags.append("Wrong stop")
                open_incident("WRONG_STOP", "MEDIUM", f"{stu['name']} released at {db.stop(cur_stop, c)['name']} instead of "
                              f"{db.stop(line['stop_id'], c)['name']}.", run_id, run["bus_id"], student_id, c, at)
            recip = ctx.get("recipient")
            allowed = allowed_recipients(student_id, run["date"], run["trip_no"], c)
            if recip == "__NONE__" or (recip is None and not allowed):
                _event(c, run, ctx, kind, "REJECTED", "No approved recipient present — not released", student_id, line["id"], badge_code)
                if not q1("SELECT id FROM incidents WHERE run_id=? AND student_id=? AND kind='NO_RECIPIENT'", (run_id, student_id), c):
                    open_incident("NO_RECIPIENT", "HIGH", f"No approved recipient for {stu['name']} ({stu['code']}) at "
                                  f"{db.stop(line['stop_id'], c)['name']}. Child stays on the bus and returns to school.", run_id, run["bus_id"], student_id, c, at)
                    notify(student_id, run_id, "CUSTODY", f"No authorized person was at the stop to receive {stu['name']} ({tlabel}, {hm}). "
                           f"The child stays with the bus crew and returns to school. Please call {db.TRANSPORT_PHONE} now.", delayed, c, rnd, at)
                return {"ok": False, "level": "error", "message": f"NOT RELEASED: no approved recipient for {stu['name']}. "
                        "Child stays on board — custody incident raised."}
            if recip is None:
                return {"ok": False, "level": "warn", "message": f"{stu['name']}: select who is receiving the child and tap Confirm handover.",
                        "needs": "recipient", "allowed": allowed, "student_id": student_id, "student_name": stu["name"]}
            match = next((a for a in allowed if a["name"] == recip), None)
            if not match:
                return reject(f"{recip} is not an authorized recipient for {stu['name']} — not released.", student_id, line["id"])
            c.execute("""UPDATE manifest SET status='COMPLETED', exit_ts=?, exit_lat=?, exit_lon=?, handover='HOME_RELEASE', recipient_name=?,
                         recipient_relation=? WHERE id=?""", (when, ctx.get("lat"), ctx.get("lon"), match["name"], match["relation"], line["id"]))
            _event(c, run, ctx, kind, "FLAGGED" if flags else "ACCEPTED", "; ".join(flags) or "HOME_RELEASE", student_id, line["id"], badge_code)
            notify(student_id, run_id, "RELEASED", f"{stu['name']} was dropped at {db.stop(line['stop_id'], c)['name']} at {hm} and handed to "
                   f"{match['name']} ({match['relation']}).", delayed, c, rnd, at)
            return {"ok": True, "level": "warn" if flags else "ok", "message": f"RELEASED: {stu['name']} → {match['name']} ({match['relation']})"
                    + (f" — ⚠ {'; '.join(flags)}" if flags else "")}
        return reject(f"Unknown action {kind}")


def manifest_for_student(stu, date, trip_no, con):
    for batch, grp, purpose in TRIPS[trip_no]["movements"]:
        if stu["batch"] == batch and stu["grp"] == grp:
            bus_id, stop_id = effective_assignment(stu, date, trip_no, con)
            if purpose == "DROPOFF" and stop_id == stu["stop_id"] and stu.get("drop_stop_id"):
                stop_id = stu["drop_stop_id"]
            return {"purpose": purpose, "bus_id": bus_id, "stop_id": stop_id, "bus_no": db.bus(bus_id, con)["bus_no"]}
    return None


def approve_transfer(manifest_id, user, role, approve=True, con=None):
    with db.tx(con) as c:
        ln = q1("SELECT * FROM manifest WHERE id=?", (manifest_id,), c)
        if not ln or ln["status"] != "TRANSFER_PENDING":
            return False
        if approve:
            c.execute("UPDATE manifest SET status='ONBOARD', note=COALESCE(note,'') || ' · transfer approved by ' || ? WHERE id=?", (user, manifest_id))
            # take the child off the original bus's manifest for this trip, if that run exists
            run = get_run(ln["run_id"], c)
            c.execute("""UPDATE manifest SET status='TRANSFERRED_OUT', note='Transferred to another bus' WHERE student_id=? AND status='EXPECTED'
                         AND run_id IN (SELECT id FROM trip_runs WHERE date=? AND trip_no=? AND id<>?)""",
                      (ln["student_id"], run["date"], run["trip_no"], run["id"]))
        else:
            c.execute("UPDATE manifest SET status='RETURNED', handover='TRANSFER_REJECTED', note=COALESCE(note,'') || ' · transfer rejected' WHERE id=?",
                      (manifest_id,))
        c.execute("""UPDATE incidents SET status='RESOLVED', resolution=?, resolved_ts=? WHERE run_id=? AND student_id=? AND kind='WRONG_BUS'
                     AND status<>'RESOLVED'""", (f"Transfer {'approved' if approve else 'rejected'} by {user}", ts(), ln["run_id"], ln["student_id"]))
        db.audit(user, role, "TRANSFER_" + ("APPROVED" if approve else "REJECTED"), "manifest", manifest_id, "", c)
        return True


def blockers(run_id, con=None):
    return [ln for ln in run_lines(run_id, con) if ln["status"] in ACTIVE_LINE]


def close_run(run_id, sweep_by, final_driver, final_supervisor, final_caretaker, lat, lon, user, role, offline=False, when=None, con=None):
    with db.tx(con) as c:
        run = get_run(run_id, c)
        if run["status"] not in ("IN_PROGRESS", "PENDING_SYNC"):
            return False, "Trip is not open.", []
        bl = blockers(run_id, c)
        if bl:
            return False, "Cannot close: every child must be accounted for.", bl
        if not sweep_by:
            return False, "Record the name of the crew member who physically swept the bus.", []
        status = "PENDING_SYNC" if offline else "CLOSED"
        c.execute("""UPDATE trip_runs SET status=?, end_ts=?, end_lat=?, end_lon=?, final_driver_id=?, final_supervisor_id=?, final_caretaker_id=?,
                     sweep_by=?, sweep_ts=?, closed_by=? WHERE id=?""",
                  (status, ts(when), lat, lon, final_driver, final_supervisor, final_caretaker, sweep_by, ts(when), user, run_id))
        db.audit(user, role, "TRIP_CLOSE_LOCAL" if offline else "TRIP_CLOSE", "trip_run", run_id, f"sweep by {sweep_by}", c)
        return True, ("Closed on the device — Pending Sync until the server validates." if offline else "Trip closed."), []


def sync_queue(items, user, role):
    """Upload offline-captured actions. Each item: {'type': 'event'|'close', ...}. Returns list of (item, result)."""
    results = []
    for it in items:
        if it["type"] == "event":
            ctx = dict(it["ctx"], offline=True, sync_state="SYNCED")
            res = process(it["run_id"], it["kind"], ctx, badge_code=it.get("badge_code"), student_id=it.get("student_id"))
            if not res["ok"] and not res.get("duplicate"):
                with db.tx() as c:
                    c.execute("UPDATE events SET sync_state='CONFLICT' WHERE client_uuid=?", (ctx["client_uuid"],))
                res["conflict"] = True
            results.append((it, res))
        elif it["type"] == "close":
            run = get_run(it["run_id"])
            bl = blockers(it["run_id"])
            if bl:
                results.append((it, {"ok": False, "level": "error", "conflict": True,
                                     "message": f"Closure rejected by server: {len(bl)} child(ren) not accounted for after sync."}))
                with db.tx() as c:
                    c.execute("UPDATE trip_runs SET status='IN_PROGRESS' WHERE id=?", (it["run_id"],))
            else:
                with db.tx() as c:
                    if run["status"] == "PENDING_SYNC":
                        c.execute("UPDATE trip_runs SET status='CLOSED' WHERE id=?", (it["run_id"],))
                    else:
                        c.execute("""UPDATE trip_runs SET status='CLOSED', end_ts=?, sweep_by=?, sweep_ts=?, closed_by=?, final_driver_id=?,
                                     final_supervisor_id=?, final_caretaker_id=? WHERE id=?""",
                                  (it["captured_ts"], it["sweep_by"], it["captured_ts"], user, it["final"][0], it["final"][1], it["final"][2], it["run_id"]))
                    db.audit(user, role, "TRIP_CLOSE_SYNCED", "trip_run", it["run_id"], "validated by server", c)
                results.append((it, {"ok": True, "level": "ok", "message": "Trip closure validated by the server — CLOSED."}))
    return results


# ------------------------------------------------------------------ absences, changes, corrections
def declare_absence(student_id, date, trip_no, reason, by, source, con=None):
    with db.tx(con) as c:
        aid = c.execute("""INSERT INTO absences(student_id, date, trip_no, reason, declared_by, source, status, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""", (student_id, date, trip_no, reason, by, source, "APPROVED", ts())).lastrowid
        c.execute("""UPDATE manifest SET status='ABSENT_DECLARED' WHERE student_id=? AND status='EXPECTED'
                     AND run_id IN (SELECT id FROM trip_runs WHERE date=? AND (? IS NULL OR trip_no=?))""", (student_id, date, trip_no, trip_no))
        db.audit(by, source.lower(), "ABSENCE_DECLARED", "student", student_id, f"{date} trip {trip_no or 'all'}: {reason}", c)
        return aid


def request_change(student_id, date, trip_no, kind, by, new_stop_id=None, new_bus_id=None, recipient_name=None, recipient_phone=None, note=""):
    cid = x("""INSERT INTO change_requests(student_id, date, trip_no, kind, new_stop_id, new_bus_id, recipient_name, recipient_phone, note,
               requested_by, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (student_id, date, trip_no, kind, new_stop_id, new_bus_id, recipient_name, recipient_phone, note, by, "PENDING", ts()))
    db.audit(by, "guardian", "CHANGE_REQUESTED", "change_request", cid, f"{kind} {date}")
    return cid


def decide_change(cr_id, approve, user, role):
    with db.tx() as c:
        cr = q1("SELECT * FROM change_requests WHERE id=?", (cr_id,), c)
        c.execute("UPDATE change_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
                  ("APPROVED" if approve else "REJECTED", user, ts(), cr_id))
        if approve and cr["kind"] == "STOP":
            c.execute("""UPDATE manifest SET stop_id=? WHERE student_id=? AND status='EXPECTED' AND run_id IN
                         (SELECT id FROM trip_runs WHERE date=? AND (? IS NULL OR trip_no=?))""",
                      (cr["new_stop_id"], cr["student_id"], cr["date"], cr["trip_no"], cr["trip_no"]))
        db.audit(user, role, "CHANGE_" + ("APPROVED" if approve else "REJECTED"), "change_request", cr_id, cr["kind"], c)
        stu = q1("SELECT name FROM students WHERE id=?", (cr["student_id"],), c)
        notify(cr["student_id"], None, "CHANGE", f"Your {cr['kind'].lower()} change request for {stu['name']} on {cr['date']} was "
               f"{'approved' if approve else 'rejected'}.", con=c)


def request_correction(manifest_id, field, new_value, reason, user, role):
    ln = q1("SELECT * FROM manifest WHERE id=?", (manifest_id,))
    cid = x("""INSERT INTO corrections(manifest_id, field, old_value, new_value, reason, requested_by, status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""", (manifest_id, field, str(ln[field]), new_value, reason, user, "PENDING", ts()))
    db.audit(user, role, "CORRECTION_REQUESTED", "manifest", manifest_id, f"{field}: {ln[field]} -> {new_value} ({reason})")
    return cid


CORRECTABLE = ("status", "entry_ts", "exit_ts", "recipient_name", "received_by", "stop_id")


def decide_correction(cid, approve, user, role):
    with db.tx() as c:
        co = q1("SELECT * FROM corrections WHERE id=?", (cid,), c)
        if approve and co["field"] in CORRECTABLE:
            c.execute(f"UPDATE manifest SET {co['field']}=? WHERE id=?", (co["new_value"], co["manifest_id"]))
        c.execute("UPDATE corrections SET status=?, decided_by=?, decided_at=? WHERE id=?", ("APPROVED" if approve else "REJECTED", user, ts(), cid))
        db.audit(user, role, "CORRECTION_" + ("APPROVED" if approve else "REJECTED"), "correction", cid,
                 f"{co['field']}: {co['old_value']} -> {co['new_value']} (original events retained)", c)


# ------------------------------------------------------------------ planning checks
def allocation_conflicts(date):
    out = []
    for s in q("""SELECT s.code, s.name FROM students s WHERE s.active=1 AND (s.bus_id IS NULL OR s.stop_id IS NULL)"""):
        out.append(("Student without bus/stop", f"{s['name']} ({s['code']})"))
    for s in q("""SELECT s.code, s.name FROM students s WHERE s.active=1 AND NOT EXISTS
                  (SELECT 1 FROM badges b WHERE b.student_id=s.id AND b.status='ACTIVE')"""):
        out.append(("No active badge", f"{s['name']} ({s['code']})"))
    for s in q("""SELECT s.code, s.name FROM students s WHERE s.active=1 AND NOT EXISTS
                  (SELECT 1 FROM recipients r WHERE r.student_id=s.id AND r.active=1)"""):
        out.append(("No authorized recipient (cannot be released at home)", f"{s['name']} ({s['code']})"))
    for s in q("""SELECT s.code, s.name, st.bus_id AS sb, s.bus_id FROM students s JOIN stops st ON st.id=s.stop_id WHERE st.bus_id<>s.bus_id"""):
        out.append(("Stop not on the student's bus route", f"{s['name']} ({s['code']})"))
    for b in db.buses():
        for no in TRIPS:
            n = len([m for m in manifest_for(b["id"], date, no) if m["status"] == "EXPECTED"])
            if n > b["capacity"]:
                out.append(("Manifest above bus capacity", f"{b['bus_no']} Trip {no}: {n} students for {b['capacity']} seats"))
    for g in q("""SELECT g.name FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id WHERE g.verified=0 GROUP BY g.id"""):
        out.append(("Guardian not verified", g["name"]))
    return out


# ------------------------------------------------------------------ simulator (demo data)
def _ctx(op, dev, stop_id, when, rnd, gps_fail=0.03, method="QR"):
    st = db.stop(stop_id)
    ok = rnd.random() > gps_fail
    return {"operator": op, "role": "supervisor", "device_id": dev, "stop_id": stop_id, "method": method,
            "lat": (st["lat"] + rnd.uniform(-.0004, .0004)) if ok else None, "lon": (st["lon"] + rnd.uniform(-.0004, .0004)) if ok else None,
            "accuracy": round(rnd.uniform(4, 25), 1) if ok else None, "gps_ok": ok, "captured_ts": ts(when), "received_ts": ts(when), "client_uuid": str(uuid.uuid4())}


def simulate_run(bus_id, date, trip_no, rnd=None, incidents=True, finish=True, upto_fraction=1.0, start_dt=None):
    """Drive a full trip through the real rules engine so all data is consistent."""
    rnd = rnd or random.Random()
    con = db.connect()
    try:
        b = db.bus(bus_id, con)
        sup = db.staff_name(b["supervisor_id"], con)
        dev = q1("SELECT id FROM devices WHERE bus_id=? AND status='ACTIVE'", (bus_id,), con)
        dev = dev["id"] if dev else None
        a, z = TRIPS[trip_no]["window"]
        t = start_dt or parse_hm(date, a) + timedelta(minutes=rnd.randint(-3, 12))
        stops = db.stops_of_bus(bus_id, con)
        first = (db.stop(SCHOOL_STOP) if any(m[2] == "DROPOFF" for m in TRIPS[trip_no]["movements"]) else stops[0])
        run_id, msg = start_run(bus_id, date, trip_no, b["driver_id"], b["supervisor_id"], b["caretaker_id"], first["lat"], first["lon"],
                                f"sup{bus_id:02d}", "supervisor", dev, when=t, con=con)
        if not run_id:
            return None
        lines = run_lines(run_id, con)
        drop = [ln for ln in lines if ln["purpose"] == "DROPOFF" and ln["status"] == "EXPECTED"]
        pick = [ln for ln in lines if ln["purpose"] == "PICKUP" and ln["status"] == "EXPECTED"]
        receivers = [r["name"] for r in q("SELECT name FROM staff WHERE role='receiving'", (), con)]
        actions = []
        # 1) drop-off children board at school
        if drop:
            for ln in drop:
                t += timedelta(seconds=rnd.randint(4, 12))
                if rnd.random() < 0.003 and incidents:
                    actions.append(("NO_SHOW", ln, SCHOOL_STOP, t))
                else:
                    actions.append(("CHECK_IN", ln, SCHOOL_STOP, t))
        # 2) route stops: release drop-offs, pick up pickups
        for stp in stops:
            t += timedelta(minutes=rnd.randint(3, 7))
            for ln in [l for l in drop if l["stop_id"] == stp["id"]]:
                t += timedelta(seconds=rnd.randint(15, 40))
                actions.append(("CHECK_OUT", ln, stp["id"], t))
            for ln in [l for l in pick if l["stop_id"] == stp["id"]]:
                t += timedelta(seconds=rnd.randint(10, 30))
                actions.append(("NO_SHOW" if (incidents and rnd.random() < 0.04) else "CHECK_IN", ln, stp["id"], t))
        # 3) arrive at school: pickups handed to receiving staff, unreleased drop-offs returned
        t += timedelta(minutes=rnd.randint(8, 15))
        for ln in pick:
            t += timedelta(seconds=rnd.randint(3, 8))
            actions.append(("CHECK_OUT", ln, SCHOOL_STOP, t))
        cut = int(len(actions) * upto_fraction)
        offline_pick = rnd.random() < 0.15 and incidents
        for i, (kind, ln, stop_id, when) in enumerate(actions[:cut]):
            cur = q1("SELECT status FROM manifest WHERE id=?", (ln["id"],), con)["status"]
            if kind == "CHECK_OUT" and cur != "ONBOARD":
                continue
            if kind == "CHECK_IN" and cur != "EXPECTED":
                continue
            manual = incidents and rnd.random() < 0.02
            ctx = _ctx(sup, dev, stop_id, when, rnd, method="MANUAL" if manual else "QR")
            if manual:
                ctx.update(manual_reason="Badge forgotten", verified_by=db.staff_name(b["caretaker_id"], con))
            if offline_pick and kind == "CHECK_IN" and rnd.random() < 0.3:
                ctx["received_ts"] = ts(when + timedelta(minutes=rnd.randint(6, 25)))
                ctx["offline"] = True
            if kind == "CHECK_OUT" and stop_id == SCHOOL_STOP:
                ctx["received_by"] = rnd.choice(receivers)
            elif kind == "CHECK_OUT":
                allowed = allowed_recipients(ln["student_id"], date, trip_no, con)
                ctx["recipient"] = rnd.choice(allowed)["name"] if allowed else "__NONE__"
            badge = q1("SELECT code FROM badges WHERE student_id=? AND status='ACTIVE'", (ln["student_id"],), con)
            if kind == "NO_SHOW" or manual:
                process(run_id, kind, ctx, student_id=ln["student_id"], con=con, rnd=rnd)
            else:
                process(run_id, kind, ctx, badge_code=badge["code"], con=con, rnd=rnd)
                if incidents and kind == "CHECK_IN" and rnd.random() < 0.01:      # accidental double scan -> rejected duplicate
                    process(run_id, kind, _ctx(sup, dev, stop_id, when + timedelta(seconds=3), rnd), badge_code=badge["code"], con=con, rnd=rnd)
        if incidents and rnd.random() < 0.08:   # someone scans an old revoked card
            old = q1("SELECT code FROM badges WHERE status='REVOKED' ORDER BY RANDOM() LIMIT 1", (), con)
            if old:
                process(run_id, "CHECK_IN", _ctx(sup, dev, stops[0]["id"], t, rnd), badge_code=old["code"], con=con, rnd=rnd)
        if finish and upto_fraction >= 1.0:
            # children not released (no recipient) are brought back to school
            for ln in run_lines(run_id, con):
                if ln["status"] == "ONBOARD":
                    t += timedelta(seconds=20)
                    ctx = _ctx(sup, dev, SCHOOL_STOP, t, rnd)
                    ctx["received_by"] = rnd.choice(receivers)
                    process(run_id, "CHECK_OUT", ctx, student_id=ln["student_id"], con=con, rnd=rnd)
            t += timedelta(minutes=2)
            end = db.stop(SCHOOL_STOP)
            sweeper = rnd.choice([db.staff_name(b["supervisor_id"], con), db.staff_name(b["caretaker_id"], con)])
            close_run(run_id, sweeper, b["driver_id"], b["supervisor_id"], b["caretaker_id"], end["lat"], end["lon"], f"sup{bus_id:02d}",
                      "supervisor", when=t, con=con)
        con.commit()
        return run_id
    finally:
        con.close()


def simulate_day(date, trips=(1, 2, 3, 4, 5), bus_ids=None, seed=None, absences=True):
    rnd = random.Random(seed)
    bus_ids = bus_ids or [b["id"] for b in db.buses()]
    if absences:
        for s in q("SELECT id FROM students WHERE active=1 ORDER BY RANDOM() LIMIT 25"):
            if not q1("SELECT id FROM absences WHERE student_id=? AND date=?", (s["id"], date)):
                declare_absence(s["id"], date, None, rnd.choice(["Sick", "Family travel", "Doctor appointment", "Parent will drop"]),
                                "guardian", "GUARDIAN")
    done = 0
    for no in trips:
        for bid in bus_ids:
            if not q1("SELECT id FROM trip_runs WHERE bus_id=? AND date=? AND trip_no=?", (bid, date, no)):
                if simulate_run(bid, date, no, rnd=rnd):
                    done += 1
    return done


def simulate_live(trip_no, bus_ids, date=None):
    rnd = random.Random()
    date = date or db.today()
    n = 0
    for bid in bus_ids:
        if open_run(bid) or q1("SELECT id FROM trip_runs WHERE bus_id=? AND date=? AND trip_no=?", (bid, date, trip_no)):
            continue
        start = db.now() - timedelta(minutes=rnd.randint(15, 35))
        if simulate_run(bid, date, trip_no, rnd=rnd, finish=False, upto_fraction=rnd.uniform(0.35, 0.75), start_dt=start):
            n += 1
    return n


def school_days_back(n):
    """Last n school days (Sunday–Thursday in Qatar), excluding today."""
    out, d = [], db.now().date()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() not in (4, 5):   # Friday, Saturday off
            out.append(d.isoformat())
    return list(reversed(out))
