"""v2.0 — student location registration and dynamic trip planning.

* Each student has a default PICKUP and DROPOFF location (address, map pin, notes, guardian). Changes need school approval
  and every version is kept (history).
* A trip plan groups the eligible students (expected to board / onboard, no confirmed absence) onto approved SAFE STOPS
  (never a raw home pin), orders the stops by estimated road travel time, checks bus capacity at every stop and the school
  arrival deadline, and records planned arrival times.
* Plans are versioned: DRAFT -> ACTIVE (approved by a supervisor / transport manager) -> SUPERSEDED. When an approved change
  (absence, stop / bus change, location change, no-show) alters the roster during the day, a revised DRAFT is generated for
  the REMAINING stops only — completed stops stay in the history and a student's destination is never removed while the
  student is on board. Activating a revision notifies the crew and the affected parents.

Travel times are estimated from straight-line distance x road factor at urban bus speed. In production the same planner
takes
road distances / times from a routing service (Google Routes, OSRM) — the interface is the `travel_min` function.
"""
import hashlib
import itertools
import json
import math
import random
from datetime import datetime, timedelta

import db
from db import SCHOOL_STOP, TRIPS, q, q1, x, ts

WALK_MAX_M = 600          # a student's registered pin must be within this walking distance of an approved safe stop
ROAD_FACTOR = 1.35        # road distance ~ straight-line x factor (urban Doha)
SPEED_KMH = 24            # average bus speed incl. junctions
DWELL_BASE_S = 60         # stop time per stop
DWELL_CHILD_S = 15        # extra per child boarding / leaving
MAX_RIDE_MIN = 60         # warning threshold for a single child's time on the bus
ACTIVE_LINE = ("EXPECTED", "ONBOARD", "TRANSFER_PENDING")
DONE_LINE = ("COMPLETED", "RETURNED", "TRANSFERRED_OUT", "ABSENT_DECLARED", "NO_SHOW", "CANCELLED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS student_locations (
  id INTEGER PRIMARY KEY, student_id INTEGER, kind TEXT, address TEXT, lat REAL, lon REAL, notes TEXT, guardian_id INTEGER,
  same_as_pickup INTEGER DEFAULT 0, status TEXT, version INTEGER, reason TEXT, requested_by TEXT, requested_ts TEXT,
  decided_by TEXT, decided_ts TEXT, decision_note TEXT);
CREATE INDEX IF NOT EXISTS ix_sloc ON student_locations(student_id, kind, status);
CREATE TABLE IF NOT EXISTS route_plans (
  id INTEGER PRIMARY KEY, date TEXT, bus_id INTEGER, trip_no INTEGER, run_id INTEGER, version INTEGER, status TEXT,
  plan_json TEXT, signature TEXT, reason TEXT, created_by TEXT, created_ts TEXT, approved_by TEXT, approved_ts TEXT,
  superseded_ts TEXT, feasible INTEGER);
CREATE INDEX IF NOT EXISTS ix_plan ON route_plans(date, bus_id, trip_no, status);
"""


def ensure_schema(con=None):
    with db.tx(con) as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(students)").fetchall()}
        if "drop_stop_id" not in cols:     # v2.0: default drop-off stop (may differ from the pickup stop)
            c.execute("ALTER TABLE students ADD COLUMN drop_stop_id INTEGER")


def sync_student_stops(student_ids=None, con=None):
    """Keep the student record (badge / ID card, daily lists) in line with the approved locations:
    students.stop_id = safe stop of the approved PICKUP location, students.drop_stop_id = safe stop of the DROPOFF location.
    Only stops on the student's own bus route are written; otherwise the record is left as it is (the planner flags it)."""
    ensure_schema(con)
    stops = safe_stops(con)
    where = "" if student_ids is None else f" AND id IN ({','.join(str(int(i)) for i in student_ids)})"
    changed = 0
    with db.tx(con) as c:
        for s in q("SELECT id, bus_id, stop_id, drop_stop_id FROM students WHERE active=1" + where, (), c):
            new = {}
            for kind, col in (("PICKUP", "stop_id"), ("DROPOFF", "drop_stop_id")):
                loc = current_location(s["id"], kind, c)
                if not loc or not _valid_qatar(loc["lat"], loc["lon"]):
                    continue
                st_, _d = nearest_safe_stop(loc["lat"], loc["lon"], stops, s["bus_id"])
                if st_ and st_["bus_id"] == s["bus_id"]:
                    new[col] = st_["id"]
            if new.get("stop_id") and new["stop_id"] != s["stop_id"]:
                c.execute("UPDATE students SET stop_id=? WHERE id=?", (new["stop_id"], s["id"]))
                changed += 1
            drop = new.get("drop_stop_id") or new.get("stop_id") or s["stop_id"]
            if drop != s["drop_stop_id"]:
                c.execute("UPDATE students SET drop_stop_id=? WHERE id=?", (drop, s["id"]))
    return changed


# ------------------------------------------------------------------ geometry / travel time
def dist_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def travel_min(a, b):
    """Estimated driving minutes between two (lat, lon) points (road factor x straight line at urban bus speed)."""
    return dist_m(a[0], a[1], b[0], b[1]) * ROAD_FACTOR / (SPEED_KMH * 1000 / 60)


# ------------------------------------------------------------------ student locations
def current_location(student_id, kind, con=None):
    """Approved default location for a movement ('PICKUP' / 'DROPOFF'); a drop-off may be 'same as pickup'."""
    row = q1("""SELECT * FROM student_locations WHERE student_id=? AND kind=? AND status='APPROVED' ORDER BY version DESC, id DESC LIMIT 1""",
             (student_id, kind), con)
    if row and kind == "DROPOFF" and row["same_as_pickup"]:
        p = current_location(student_id, "PICKUP", con)
        if p:
            p = dict(p)
            p.update(kind="DROPOFF", same_as_pickup=1, id=row["id"], version=row["version"])
            return p
    return row


def pending_locations(con=None):
    return q("""SELECT l.*, s.name, s.code FROM student_locations l JOIN students s ON s.id=l.student_id WHERE l.status='PENDING' ORDER BY l.id""",
             (), con)


def location_history(student_id, con=None):
    return q("SELECT * FROM student_locations WHERE student_id=? ORDER BY id DESC", (student_id,), con)


def request_location(student_id, kind, address, lat, lon, notes, guardian_id, same_as_pickup, reason, user, role, auto_approve=False):
    """Submit a new default location. School approval is required (auto_approve only for the initial enrolment by the office)."""
    with db.tx() as c:
        ensure_schema(c)
        ver = (q1("SELECT MAX(version) v FROM student_locations WHERE student_id=? AND kind=?", (student_id, kind), c)["v"] or 0) + 1
        lid = x("""INSERT INTO student_locations(student_id, kind, address, lat, lon, notes, guardian_id, same_as_pickup, status, version, reason,
                   requested_by, requested_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (student_id, kind, address, lat, lon, notes, guardian_id, 1 if same_as_pickup else 0, "PENDING", ver, reason, user, ts()), c)
        db.audit(user, role, "LOCATION_REQUESTED", "student", student_id, f"{kind} v{ver}: {address} ({lat}, {lon}) — {reason}", c)
    if auto_approve:
        decide_location(lid, True, user, role, "initial enrolment")
    return lid


def decide_location(loc_id, approve, user, role, note=""):
    with db.tx() as c:
        row = q1("SELECT * FROM student_locations WHERE id=?", (loc_id,), c)
        if not row or row["status"] != "PENDING":
            return False
        if approve:
            c.execute("UPDATE student_locations SET status='SUPERSEDED' WHERE student_id=? AND kind=? AND status='APPROVED'",
                      (row["student_id"], row["kind"]))
        c.execute("UPDATE student_locations SET status=?, decided_by=?, decided_ts=?, decision_note=? WHERE id=?",
                  ("APPROVED" if approve else "REJECTED", user, ts(), note, loc_id))
        db.audit(user, role, "LOCATION_APPROVED" if approve else "LOCATION_REJECTED", "student", row["student_id"],
                 f"{row['kind']} v{row['version']}: {row['address']} {note}", c)
        linked = None
        if row["kind"] == "PICKUP":   # a "drop-off same as pickup" submitted together follows the pickup decision
            linked = q1("""SELECT id FROM student_locations WHERE student_id=? AND kind='DROPOFF' AND status='PENDING' AND same_as_pickup=1
                           AND requested_by=? AND requested_ts=?""", (row["student_id"], row["requested_by"], row["requested_ts"]), c)
    if linked:
        decide_location(linked["id"], approve, user, role, note or "with the pickup location")
    if approve:
        sync_student_stops([row["student_id"]])
    return True


def safe_stops(con=None):
    """Approved safe boarding / handover points (the stops table)."""
    return q("SELECT s.*, b.bus_no FROM stops s LEFT JOIN buses b ON b.id=s.bus_id WHERE s.lat IS NOT NULL", (), con)


def nearest_safe_stop(lat, lon, stops, bus_id=None):
    """Nearest approved stop within walking distance; the bus's own stops are preferred (a 150 m bonus)."""
    best = None
    for s in stops:
        d = dist_m(lat, lon, s["lat"], s["lon"])
        score = d - (150 if bus_id and s["bus_id"] == bus_id else 0)
        if d <= WALK_MAX_M and (best is None or score < best[0]):
            best = (score, s, d)
    return (best[1], best[2]) if best else (None, None)


def location_check(student, con=None):
    """Flags for one student: missing / invalid location, no safe stop within walking distance, pending change."""
    stops = safe_stops(con)
    out = {}
    for kind in ("PICKUP", "DROPOFF"):
        loc = current_location(student["id"], kind, con)
        if not loc or loc["lat"] is None or loc["lon"] is None:
            out[kind] = {"flag": "MISSING", "loc": loc, "stop": None, "dist": None}
            continue
        if not (24.4 <= loc["lat"] <= 26.3 and 50.7 <= loc["lon"] <= 51.7):
            out[kind] = {"flag": "INVALID (outside Qatar)", "loc": loc, "stop": None, "dist": None}
            continue
        s, d = nearest_safe_stop(loc["lat"], loc["lon"], stops, student["bus_id"])
        out[kind] = {"flag": "OK" if s else f"NO SAFE STOP within {WALK_MAX_M} m", "loc": loc, "stop": s, "dist": d}
    out["pending"] = q1("SELECT COUNT(*) n FROM student_locations WHERE student_id=? AND status='PENDING'", (student["id"],), con)["n"]
    return out


def _valid_qatar(lat, lon):
    return lat is not None and lon is not None and 24.4 <= lat <= 26.3 and 50.7 <= lon <= 51.7


def location_overview(con=None):
    """One row per active student: approved pickup / drop-off, nearest safe stop, walking distance and a flag (bulk, few queries)."""
    ensure_schema(con)
    stops = safe_stops(con)
    stop_by_id = {s["id"]: s for s in stops}
    locs = {}
    for r in q("""SELECT * FROM student_locations WHERE status='APPROVED' ORDER BY version""", (), con):
        locs[(r["student_id"], r["kind"])] = r
    pend = {r["student_id"]: r["n"] for r in q("SELECT student_id, COUNT(*) n FROM student_locations WHERE status='PENDING' GROUP BY 1", (), con)}
    out = []
    for s in q("""SELECT s.*, b.bus_no FROM students s LEFT JOIN buses b ON b.id=s.bus_id WHERE s.active=1 ORDER BY b.bus_no, s.name""", (), con):
        row = {"student_id": s["id"], "Code": s["code"], "Student": s["name"], "Class": f"{s['class_name'] or ''}{s['section'] or ''}",
               "Bus": s["bus_no"], "bus_id": s["bus_id"], "Batch": s["batch"], "Group": s["grp"],
               "Assigned stop": (stop_by_id.get(s["stop_id"]) or {}).get("name", "—"), "Pending": pend.get(s["id"], 0)}
        issues = []
        for kind, lab in (("PICKUP", "Pickup"), ("DROPOFF", "Drop-off")):
            loc = locs.get((s["id"], kind))
            if loc and kind == "DROPOFF" and loc["same_as_pickup"]:
                p = locs.get((s["id"], "PICKUP"))
                row["Drop-off"] = "Same as pickup"
                loc = dict(p, same_as_pickup=1) if p else None
                if not p:
                    loc = None
            elif kind == "DROPOFF":
                row["Drop-off"] = (loc["address"] or "—") if loc else "—"
            if kind == "PICKUP":
                row["Pickup"] = (loc["address"] or "—") if loc else "—"
            if not loc or loc["lat"] is None:
                issues.append(f"{lab}: missing")
                row[f"{lab} stop"] = "—"
                continue
            if not _valid_qatar(loc["lat"], loc["lon"]):
                issues.append(f"{lab}: invalid pin")
                row[f"{lab} stop"] = "—"
                continue
            st_, d = nearest_safe_stop(loc["lat"], loc["lon"], stops, s["bus_id"])
            if not st_:
                issues.append(f"{lab}: no safe stop ≤{WALK_MAX_M} m")
                row[f"{lab} stop"] = "—"
            else:
                row[f"{lab} stop"] = f"{st_['name']} · {d:.0f} m"
        row["Status"] = "OK" if not issues else "; ".join(issues)
        out.append(row)
    return out


def seed_locations(force=False, seed=7):
    """Initial enrolment data for the demo dataset: a default home pin near each student's assigned stop
    (a few near a neighbouring stop, two missing and one invalid so the correction workflow can be shown)."""
    ensure_schema()
    if not force and q1("SELECT COUNT(*) n FROM student_locations")["n"]:
        return 0
    rnd = random.Random(seed)
    stops = {s["id"]: s for s in q("SELECT * FROM stops")}
    by_bus = {}
    for s in stops.values():
        by_bus.setdefault(s["bus_id"], []).append(s)
    studs = q("SELECT * FROM students WHERE active=1 ORDER BY id")
    rows = []
    for i, s in enumerate(studs):
        if s["stop_id"] not in stops:
            continue
        if i in (7, 58):                 # missing location -> flagged before departure
            continue
        base = stops[s["stop_id"]]
        if i % 12 == 5 and len(by_bus.get(s["bus_id"], [])) > 1:      # lives nearer a neighbouring stop on the same route
            base = rnd.choice([o for o in by_bus[s["bus_id"]] if o["id"] != s["stop_id"]])
        r_m, ang = rnd.uniform(40, 260), rnd.uniform(0, 2 * math.pi)
        lat = base["lat"] + (r_m * math.cos(ang)) / 111320
        lon = base["lon"] + (r_m * math.sin(ang)) / (111320 * math.cos(math.radians(base["lat"])))
        if i == 95:                      # pin far from any safe stop -> "no safe stop" flag
            lat, lon = lat + 0.012, lon + 0.012
        g = q1("SELECT guardian_id FROM student_guardians WHERE student_id=? ORDER BY is_primary DESC LIMIT 1", (s["id"],))
        zone, street, villa = 20 + (s["id"] % 40), 800 + (s["id"] * 7) % 200, 1 + (s["id"] * 13) % 90
        addr = f"Villa {villa}, Street {street}, Zone {zone}, Doha"
        rows.append((s["id"], "PICKUP", addr, round(lat, 6), round(lon, 6), "Main gate", g["guardian_id"] if g else None, 0))
        if i % 9 == 4 and len(by_bus.get(s["bus_id"], [])) > 1:        # different afternoon address (e.g. grandparents)
            o = rnd.choice(by_bus[s["bus_id"]])
            dl = o["lat"] + rnd.uniform(-0.0015, 0.0015)
            dn = o["lon"] + rnd.uniform(-0.0015, 0.0015)
            rows.append((s["id"], "DROPOFF", f"Building {villa + 3}, Street {street + 11}, Zone {zone + 1}, Doha", round(dl, 6), round(dn, 6),
                         "Grandparents' home (afternoons)", g["guardian_id"] if g else None, 0))
        else:
            rows.append((s["id"], "DROPOFF", None, None, None, "Same as pickup", g["guardian_id"] if g else None, 1))
    with db.tx() as c:
        c.executemany("""INSERT INTO student_locations(student_id, kind, address, lat, lon, notes, guardian_id, same_as_pickup, status, version,
                         reason, requested_by, requested_ts, decided_by, decided_ts) VALUES (?,?,?,?,?,?,?,?,'APPROVED',1,'Transport enrolment',
                         'system',?, 'system', ?)""", [r + (ts(), ts()) for r in rows])
    sync_student_stops()
    return len(rows)


# ------------------------------------------------------------------ roster
def _eligible(bus_id, date, trip_no, run=None, con=None):
    """Students the bus must serve: pickups still to board, drop-offs scheduled to board or on board. Absences / no-shows excluded."""
    if run:
        lines = q("""SELECT m.*, s.name, s.code, s.bus_id AS home_bus FROM manifest m JOIN students s ON s.id=m.student_id WHERE m.run_id=?""",
                  (run["id"],), con)
        out = []
        for ln in lines:
            out.append({"student_id": ln["student_id"], "name": ln["name"], "code": ln["code"], "purpose": ln["purpose"],
                        "status": ln["status"], "stop_id": ln["stop_id"], "batch": ln["batch"], "grp": ln["grp"]})
        return out
    import engine as E
    plan = E.manifest_for(bus_id, date, trip_no, con)
    return [dict(p, status=p["status"]) for p in plan]


def _approved_stop_change(stu, date, trip_no, con=None):
    return q1("""SELECT id FROM change_requests WHERE student_id=? AND date=? AND status='APPROVED' AND kind IN ('BUS','STOP')
                 AND (trip_no IS NULL OR trip_no=?)""", (stu["id"], date, trip_no), con) is not None


def build_roster(bus_id, date, trip_no, run=None, con=None):
    """Roster with each student's movement, registered location, assigned safe stop and flags."""
    stops = safe_stops(con)
    stop_by_id = {s["id"]: s for s in stops}
    roster, flags = [], []
    for r in _eligible(bus_id, date, trip_no, run, con):
        stu = q1("SELECT * FROM students WHERE id=?", (r["student_id"],), con)
        kind = "PICKUP" if r["purpose"] == "PICKUP" else "DROPOFF"
        loc = current_location(r["student_id"], kind, con)
        item = dict(r, kind=kind, loc_address=loc["address"] if loc else None, loc_lat=loc["lat"] if loc else None,
                    loc_lon=loc["lon"] if loc else None, flag=None)
        if r["status"] in DONE_LINE:
            item["plan_stop"] = r["stop_id"]
            roster.append(item)
            continue
        locked = r["status"] in ("ONBOARD", "TRANSFER_PENDING") and r["purpose"] == "PICKUP"
        override = stu and r["stop_id"] and r["stop_id"] != stu["stop_id"] and _approved_stop_change(stu, date, trip_no, con)
        if locked:                      # on board for school: destination is the school
            item["plan_stop"] = SCHOOL_STOP
        elif override:                  # an approved stop / bus change for this day wins over the default location
            item["plan_stop"] = r["stop_id"]
            item["override"] = True
        elif not loc or loc["lat"] is None:
            item["plan_stop"] = r["stop_id"]
            item["flag"] = "Missing location — using assigned stop"
        else:
            s, d = nearest_safe_stop(loc["lat"], loc["lon"], stops, bus_id)
            if s is None:
                item["plan_stop"] = r["stop_id"]
                item["flag"] = f"No safe stop within {WALK_MAX_M} m of registered pin — using assigned stop"
            else:
                item["plan_stop"] = s["id"]
                item["walk_m"] = round(d)
        if item["flag"]:
            flags.append({"student_id": r["student_id"], "name": r["name"], "code": r["code"], "movement": kind.title(), "issue": item["flag"],
                          "assigned stop": (stop_by_id.get(r["stop_id"]) or {}).get("name", "—")})
        roster.append(item)
    return roster, flags


def signature(roster):
    """Fingerprint of what the plan depends on: who still has to be served and at which stop."""
    key = sorted((r["student_id"], r["purpose"], r["plan_stop"], r["status"] in DONE_LINE) for r in roster)
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ planning
def _hm(dt):
    return dt.strftime("%H:%M")


def plan_trip(bus_id, date, trip_no, run=None, start_dt=None, start_pos=None, frozen=None, con=None):
    """Compute an ordered stop sequence. `frozen` = completed stops kept from the previous plan (in progress replanning)."""
    bus = db.bus(bus_id, con)
    cap = bus["capacity"] or 40
    school = db.school_pos(con)
    stop_by_id = {s["id"]: s for s in safe_stops(con)}
    roster, flags = build_roster(bus_id, date, trip_no, run, con)
    win_s, win_e = TRIPS[trip_no]["window"]
    day = datetime.strptime(date, "%Y-%m-%d")
    deadline = datetime.combine(day.date(), datetime.strptime(win_e, "%H:%M").time())
    t0 = start_dt or datetime.combine(day.date(), datetime.strptime(win_s, "%H:%M").time())
    late_note = None
    if run and run.get("start_ts"):
        # a trip that started late is already late: the planner keeps the same allowed journey length from the actual start
        # (the delay itself shows on the punctuality report)
        started = datetime.strptime(run["start_ts"][:16], "%Y-%m-%d %H:%M")
        sched = datetime.combine(day.date(), datetime.strptime(win_s, "%H:%M").time())
        if started > sched + timedelta(minutes=5):
            deadline = deadline + (started - sched)
            late_note = f"Trip started {_hm(started)}, {int((started - sched).total_seconds() // 60)} min after the scheduled {win_s}: " \
                        f"remaining stops are planned against {_hm(deadline)} (same journey length); the delay is on the punctuality report"

    active = [r for r in roster if r["status"] in ACTIVE_LINE]
    onboard = [r for r in active if r["status"] in ("ONBOARD", "TRANSFER_PENDING")]
    board_at_school = [r for r in active if r["purpose"] == "DROPOFF" and r["status"] == "EXPECTED"]
    has_pickup = any(r["purpose"] == "PICKUP" for r in roster)
    starts_at_school = bool(board_at_school) or (not has_pickup) or (run is None and any(r["purpose"] == "DROPOFF" for r in roster))
    if run and start_pos is None:
        starts_at_school = bool(board_at_school)

    # work per route stop
    work = {}
    for r in active:
        sid = r["plan_stop"]
        if sid in (None, SCHOOL_STOP):
            continue
        w = work.setdefault(sid, {"board": [], "exit": []})
        if r["purpose"] == "PICKUP" and r["status"] == "EXPECTED":
            w["board"].append(r)
        elif r["purpose"] == "DROPOFF":
            w["exit"].append(r)
    route_ids = list(work)

    def simulate(order):
        t = t0
        pos = start_pos or (school if starts_at_school else None)
        occ = len(onboard)
        seq, viol, warn = [], [], []
        if starts_at_school and board_at_school:
            occ += len(board_at_school)
            t += timedelta(seconds=DWELL_BASE_S + DWELL_CHILD_S * len(board_at_school))
            seq.append({"stop_id": SCHOOL_STOP, "name": "School (boarding)", "lat": school[0], "lon": school[1],
                        "board": [_brief(r) for r in board_at_school], "exit": [], "eta": _hm(t0), "occ_after": occ})
            if occ > cap:
                viol.append(f"Bus over capacity at School: {occ} / {cap}")
        board_time = {r["student_id"]: t for r in board_at_school}
        for r in onboard:
            board_time[r["student_id"]] = t0
        for sid in order:
            s = stop_by_id.get(sid)
            if not s:
                continue
            if pos is not None:
                t += timedelta(minutes=travel_min(pos, (s["lat"], s["lon"])))
            eta = t
            w = work[sid]
            occ -= len(w["exit"])
            occ += len(w["board"])
            for r in w["exit"]:
                if r["student_id"] in board_time:
                    ride = (eta - board_time[r["student_id"]]).total_seconds() / 60
                    if ride > MAX_RIDE_MIN:
                        warn.append(f"{r['name']} would ride {ride:.0f} min (over {MAX_RIDE_MIN})")
            for r in w["board"]:
                board_time[r["student_id"]] = eta
            if occ > cap:
                viol.append(f"Bus over capacity after {s['name']}: {occ} / {cap}")
            t += timedelta(seconds=DWELL_BASE_S + DWELL_CHILD_S * (len(w["exit"]) + len(w["board"])))
            seq.append({"stop_id": sid, "name": s["name"], "lat": s["lat"], "lon": s["lon"], "board": [_brief(r) for r in w["board"]],
                        "exit": [_brief(r) for r in w["exit"]], "eta": _hm(eta), "occ_after": occ})
            pos = (s["lat"], s["lon"])
        school_arrival = None
        to_school = [r for r in active if r["purpose"] == "PICKUP"]
        if to_school:
            if pos is not None:
                t += timedelta(minutes=travel_min(pos, school))
            school_arrival = t
            seq.append({"stop_id": SCHOOL_STOP, "name": "School (hand over)", "lat": school[0], "lon": school[1], "board": [],
                        "exit": [_brief(r) for r in to_school], "eta": _hm(t), "occ_after": occ - len(to_school)})
            if t > deadline:
                viol.append(f"Arrives at school {_hm(t)}, after the {_hm(deadline)} deadline")
            for r in to_school:
                ride = (t - board_time.get(r["student_id"], t)).total_seconds() / 60
                if ride > MAX_RIDE_MIN:
                    warn.append(f"{r['name']} would ride {ride:.0f} min (over {MAX_RIDE_MIN})")
        total = (t - t0).total_seconds() / 60
        return seq, viol, warn, school_arrival, total

    best = None
    cands = itertools.permutations(route_ids) if len(route_ids) <= 7 else [_nearest_neighbour(route_ids, stop_by_id, start_pos or school)]
    for order in cands:
        seq, viol, warn, arr, total = simulate(list(order))
        key = (len(viol), total)
        if best is None or key < best[0]:
            best = (key, list(order), seq, viol, warn, arr, total)
    if best is None:
        seq, viol, warn, arr, total = simulate([])
        best = ((len(viol), total), [], seq, viol, warn, arr, total)
    _, order, seq, viol, warn, arr, total = best
    if late_note:
        warn = warn + [late_note]
    frozen = frozen or []
    for i, s in enumerate(frozen + seq, 1):
        s["seq"] = i
    return {
        "bus_id": bus_id, "bus_no": bus["bus_no"], "capacity": cap, "date": date, "trip_no": trip_no, "label": TRIPS[trip_no]["label"],
        "window": [win_s, _hm(deadline)], "start": _hm(t0), "school_arrival": _hm(arr) if arr else None, "total_min": round(total, 1),
        "max_occupancy": max([s["occ_after"] for s in seq] + [len(onboard)]) if seq else len(onboard),
        "stops": frozen + seq, "completed_count": len(frozen), "violations": viol, "warnings": sorted(set(warn)), "flags": flags,
        "students": len([r for r in roster if r["status"] not in ("ABSENT_DECLARED", "NO_SHOW", "CANCELLED")]),
        "excluded": [{"name": r["name"], "status": r["status"]} for r in roster if r["status"] in ("ABSENT_DECLARED", "NO_SHOW", "CANCELLED")],
        "signature": signature(roster), "method": f"Estimated road time (straight line x {ROAD_FACTOR} at {SPEED_KMH} km/h)",
    }


def _brief(r):
    return {"student_id": r["student_id"], "name": r["name"], "code": r["code"], "purpose": r["purpose"],
            "loc": [r.get("loc_lat"), r.get("loc_lon")], "address": r.get("loc_address"), "walk_m": r.get("walk_m")}


def _nearest_neighbour(ids, stop_by_id, start):
    left, order, pos = list(ids), [], start
    while left:
        nxt = min(left, key=lambda i: dist_m(pos[0], pos[1], stop_by_id[i]["lat"], stop_by_id[i]["lon"]))
        order.append(nxt)
        left.remove(nxt)
        pos = (stop_by_id[nxt]["lat"], stop_by_id[nxt]["lon"])
    return order


# ------------------------------------------------------------------ versions
def plans(date, bus_id, trip_no, con=None):
    ensure_schema(con)
    return q("SELECT * FROM route_plans WHERE date=? AND bus_id=? AND trip_no=? ORDER BY version DESC", (date, bus_id, trip_no), con)


def active_plan(date, bus_id, trip_no, con=None):
    ensure_schema(con)
    return q1("SELECT * FROM route_plans WHERE date=? AND bus_id=? AND trip_no=? AND status='ACTIVE' ORDER BY version DESC LIMIT 1",
              (date, bus_id, trip_no), con)


def draft_plan(date, bus_id, trip_no, con=None):
    ensure_schema(con)
    return q1("SELECT * FROM route_plans WHERE date=? AND bus_id=? AND trip_no=? AND status='DRAFT' ORDER BY version DESC LIMIT 1",
              (date, bus_id, trip_no), con)


def _run_for(date, bus_id, trip_no, con=None):
    return q1("SELECT * FROM trip_runs WHERE date=? AND bus_id=? AND trip_no=?", (date, bus_id, trip_no), con)


def stop_progress(plan, run, con=None):
    """Annotate plan stops with actual arrival and status (Done / Next / Pending) from the trip's scans."""
    stops = [dict(s) for s in plan["stops"]]
    if not run:
        for s in stops:
            s.setdefault("actual", None)
            s["status"] = s.get("status") or "Planned"
        return stops
    lines = {ln["student_id"]: ln for ln in q("SELECT * FROM manifest WHERE run_id=?", (run["id"],), con)}
    next_set = False
    for s in stops:
        sid = s["stop_id"]
        kind_f = ""
        if sid == SCHOOL_STOP:   # boarding at school = first check-in there; hand-over = first check-out there
            kind_f = " AND kind='CHECK_IN'" if s["board"] else " AND kind='CHECK_OUT'"
        ev = q1("""SELECT MIN(captured_ts) t FROM events WHERE run_id=? AND result IN ('ACCEPTED','FLAGGED') AND COALESCE(stop_id,0)=?""" + kind_f,
                (run["id"], sid or 0), con)
        if s.get("frozen"):
            s["status"] = "Done"
            s["actual"] = s.get("actual") or (ev["t"] or "")[11:16] or None
            continue
        s["actual"] = (ev["t"] or "")[11:16] or None
        pending = 0
        for st_ in s["board"]:
            ln = lines.get(st_["student_id"])
            if ln and ln["status"] == "EXPECTED":
                pending += 1
        for st_ in s["exit"]:
            ln = lines.get(st_["student_id"])
            if ln and ln["status"] in ("ONBOARD", "EXPECTED", "TRANSFER_PENDING"):
                pending += 1
        if pending == 0 and (s["board"] or s["exit"]):
            s["status"] = "Done"
        elif not next_set:
            s["status"] = "Next"
            next_set = True
        else:
            s["status"] = "Pending"
    return stops


def create_draft(date, bus_id, trip_no, user, role, reason="Initial plan", con=None):
    """Generate a DRAFT plan. During a trip only the remaining stops are recalculated; completed stops are kept."""
    ensure_schema(con)
    run = _run_for(date, bus_id, trip_no, con)
    in_progress = run and run["status"] in ("IN_PROGRESS", "PENDING_SYNC")
    frozen, start_dt, start_pos = [], None, None
    act = active_plan(date, bus_id, trip_no, con)
    if in_progress:
        if act:
            prev = json.loads(act["plan_json"])
            for s in stop_progress(prev, run, con):
                if s["status"] == "Done":
                    s = dict(s, frozen=True, planned_eta=s["eta"], eta=s.get("actual") or s["eta"])
                    frozen.append(s)
        ev = q1("""SELECT lat, lon FROM events WHERE run_id=? AND lat IS NOT NULL AND result<>'REJECTED' ORDER BY captured_ts DESC LIMIT 1""",
                (run["id"],), con)
        start_pos = (ev["lat"], ev["lon"]) if ev else ((run["start_lat"], run["start_lon"]) if run["start_lat"] else None)
        start_dt = db.now()
    p = plan_trip(bus_id, date, trip_no, run if run else None, start_dt=start_dt, start_pos=start_pos, frozen=frozen, con=con)
    if frozen:  # never re-list a stop that has already been completed
        done_ids = {s["stop_id"] for s in frozen if s["stop_id"] != SCHOOL_STOP}
        p["stops"] = frozen + [s for s in p["stops"][len(frozen):] if s["stop_id"] not in done_ids or s["board"] or s["exit"]]
        for i, s in enumerate(p["stops"], 1):
            s["seq"] = i
    with db.tx(con) as c:
        c.execute("UPDATE route_plans SET status='DISCARDED', superseded_ts=? WHERE date=? AND bus_id=? AND trip_no=? AND status='DRAFT'",
                  (ts(), date, bus_id, trip_no))
        ver = (q1("SELECT MAX(version) v FROM route_plans WHERE date=? AND bus_id=? AND trip_no=?", (date, bus_id, trip_no), c)["v"] or 0) + 1
        pid = x("""INSERT INTO route_plans(date, bus_id, trip_no, run_id, version, status, plan_json, signature, reason, created_by, created_ts, feasible)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date, bus_id, trip_no, run["id"] if run else None, ver, "DRAFT", json.dumps(p, default=str), p["signature"], reason, user, ts(),
                 0 if p["violations"] else 1), c)
        db.audit(user, role, "ROUTE_DRAFT", "route_plan", pid, f"{p['bus_no']} {date} trip {trip_no} v{ver}: {reason}", c)
    return pid


def approve_plan(plan_id, user, role, con=None, announce=True):
    """Activate a draft (supervisor / manager / admin). Infeasible plans can't be activated. Applies stop assignments to the trip,
    notifies the crew and the parents whose stop or time changed."""
    import engine as E
    with db.tx(con) as c:
        pl = q1("SELECT * FROM route_plans WHERE id=?", (plan_id,), c)
        if not pl or pl["status"] != "DRAFT":
            return False, "Only a draft plan can be approved."
        if role not in ("admin", "manager", "supervisor"):
            return False, "Only a supervisor or the transport manager can approve a route."
        p = json.loads(pl["plan_json"])
        if p["violations"]:
            return False, "This plan breaks a rule (" + "; ".join(p["violations"]) + ") — it cannot be activated."
        prev = q1("SELECT * FROM route_plans WHERE date=? AND bus_id=? AND trip_no=? AND status='ACTIVE'", (pl["date"], pl["bus_id"], pl["trip_no"]), c)
        c.execute("UPDATE route_plans SET status='SUPERSEDED', superseded_ts=? WHERE date=? AND bus_id=? AND trip_no=? AND status='ACTIVE'",
                  (ts(), pl["date"], pl["bus_id"], pl["trip_no"]))
        c.execute("UPDATE route_plans SET status='ACTIVE', approved_by=?, approved_ts=? WHERE id=?", (user, ts(), plan_id))
        run = _run_for(pl["date"], pl["bus_id"], pl["trip_no"], c)
        changed = _changed_students(json.loads(prev["plan_json"]) if prev else None, p)
        if run:
            c.execute("UPDATE route_plans SET run_id=? WHERE id=?", (run["id"], plan_id))
            apply_to_run(p, run, c)
        db.audit(user, role, "ROUTE_APPROVED", "route_plan", plan_id,
                 f"{p['bus_no']} {pl['date']} trip {pl['trip_no']} v{pl['version']} ({pl['reason']}); {len(changed)} student(s) affected", c)
    if prev and announce:   # a revision: tell the crew and the affected parents
        E.send_bus_message(pl["bus_id"], run["id"] if run else None,
                           f"Route revised (v{pl['version']}): {pl['reason']}. Next stops: " +
                           " → ".join(s["name"] for s in p["stops"] if not s.get("frozen"))[:240], user, role)
        stop_names = {s["stop_id"]: (s["name"], s["eta"]) for s in p["stops"]}
        for sid_, (stop_id, _old) in changed.items():
            nm, eta = stop_names.get(stop_id, ("the school", ""))
            E.notify(sid_, run["id"] if run else None, "ROUTE_CHANGE",
                     f"Route update for {p['bus_no']} Trip {pl['trip_no']}: your child's stop is now {nm}" + (f", planned around {eta}" if eta else "") +
                     f". Reason: {pl['reason']}.")
    return True, f"Route v{pl['version']} is now active." + ((f" Crew and {len(changed)} parent(s) notified." if changed else " Crew notified; no child's stop or time changed by more than 10 min.")
                                                          if prev and announce else "")


def _changed_students(old, new):
    """student_id -> (new stop, old stop) for students whose stop moved or ETA shifted > 10 min."""
    def idx(p):
        out = {}
        for s in (p or {}).get("stops", []):
            for st_ in s["board"] + s["exit"]:
                if s["stop_id"] != SCHOOL_STOP or st_["student_id"] not in out:   # the child's own stop, not the school end
                    out[st_["student_id"]] = (s["stop_id"], s["eta"])
        return out
    if old is None:
        return {}
    o, n = idx(old), idx(new)
    ch = {}
    for sid_, (stop, eta) in n.items():
        if sid_ not in o:
            continue
        ostop, oeta = o[sid_]
        try:
            shift = abs((datetime.strptime(eta, "%H:%M") - datetime.strptime(oeta, "%H:%M")).total_seconds()) / 60
        except (TypeError, ValueError):
            shift = 0
        if stop != ostop or shift > 10:
            ch[sid_] = (stop, ostop)
    return ch


def reject_plan(plan_id, user, role, note=""):
    with db.tx() as c:
        c.execute("UPDATE route_plans SET status='REJECTED', approved_by=?, approved_ts=?, reason=reason || ' · rejected: ' || ? WHERE id=? AND status='DRAFT'",
                  (user, ts(), note or "—", plan_id))
        db.audit(user, role, "ROUTE_REJECTED", "route_plan", plan_id, note, c)


def apply_to_run(p, run, con):
    """Point each still-active manifest line at the stop the approved plan uses (completed lines are never touched)."""
    for s in p["stops"]:
        if s.get("frozen") or s["stop_id"] == SCHOOL_STOP:
            continue
        for st_ in s["board"] + s["exit"]:
            con.execute("""UPDATE manifest SET stop_id=? WHERE run_id=? AND student_id=? AND status IN ('EXPECTED','ONBOARD','TRANSFER_PENDING')""",
                        (s["stop_id"], run["id"], st_["student_id"]))


def attach_to_started_run(run, user, role):
    """When a trip starts: use the approved plan for that trip (or approve the current draft if it is valid)."""
    ensure_schema()
    act = active_plan(run["date"], run["bus_id"], run["trip_no"])
    if act and act["run_id"] and act["run_id"] != run["id"]:     # belonged to a trip that was reset / cancelled
        with db.tx() as c:
            c.execute("UPDATE route_plans SET status='SUPERSEDED', superseded_ts=? WHERE id=?", (ts(), act["id"]))
        act = None
    if not act:
        d = draft_plan(run["date"], run["bus_id"], run["trip_no"])
        if d and d["run_id"] and d["run_id"] != run["id"]:
            d = None
        if not d:
            create_draft(run["date"], run["bus_id"], run["trip_no"], user, role, "Plan generated at trip start")
            d = draft_plan(run["date"], run["bus_id"], run["trip_no"])
        ok, _m = approve_plan(d["id"], user, role)
        act = active_plan(run["date"], run["bus_id"], run["trip_no"])
        if not act:
            return None
    else:
        # approved earlier for the scheduled time: if the bus actually leaves at a different time, re-time the same route from the
        # real start so planned arrival times stay meaningful (kept as a new version; no parent alerts for a pure re-timing)
        p = json.loads(act["plan_json"])
        started = (run["start_ts"] or "")[11:16]
        try:
            gap = abs((datetime.strptime(started, "%H:%M") - datetime.strptime(p["start"], "%H:%M")).total_seconds()) / 60
        except ValueError:
            gap = 0
        if gap > 10 and role in ("admin", "manager", "supervisor"):
            pid = create_draft(run["date"], run["bus_id"], run["trip_no"], user, role, f"Re-timed at trip start ({started}, planned {p['start']})")
            ok, _m = approve_plan(pid, user, role, announce=False)
            if ok:
                act = active_plan(run["date"], run["bus_id"], run["trip_no"])
    with db.tx() as c:
        c.execute("UPDATE route_plans SET run_id=? WHERE id=?", (run["id"], act["id"]))
        apply_to_run(json.loads(act["plan_json"]), run, c)
    return q1("SELECT * FROM route_plans WHERE id=?", (act["id"],))


def check_replan(date, bus_id, trip_no, user="system", role="system"):
    """If approved changes altered the roster (absence, stop/bus change, location change, no-show...), prepare a revised DRAFT
    for the remaining stops. Returns the draft row (existing or new) or None if the active plan is still valid."""
    act = active_plan(date, bus_id, trip_no)
    if not act:
        return None
    run = _run_for(date, bus_id, trip_no)
    if run and run["status"] == "CLOSED":
        return None
    roster, _ = build_roster(bus_id, date, trip_no, run if run else None)
    sig = signature(roster)
    p = json.loads(act["plan_json"])
    if sig == act["signature"]:
        return None
    # only routing-relevant differences count: a student still to be served whose stop moved / who left / who was added
    def todo(rows):
        return sorted((r["student_id"], r["plan_stop"]) for r in rows if r["status"] in ACTIVE_LINE and
                      not (r["status"] in ("ONBOARD", "TRANSFER_PENDING") and r["purpose"] == "PICKUP"))
    planned = sorted((st_["student_id"], s["stop_id"]) for s in p["stops"] if not s.get("frozen") and s["stop_id"] != SCHOOL_STOP
                     for st_ in s["board"] + s["exit"])
    lines = {ln["student_id"]: ln for ln in q("SELECT * FROM manifest WHERE run_id=?", (run["id"],))} if run else {}
    def still_todo(sid_):
        """Normal progress (boarded for school, dropped off, no-show at the stop) is not a change; an absence / cancellation is."""
        ln = lines.get(sid_)
        if not ln:
            return True
        if ln["status"] in ("COMPLETED", "RETURNED", "TRANSFERRED_OUT", "NO_SHOW"):
            return False
        return not (ln["status"] in ("ONBOARD", "TRANSFER_PENDING") and ln["purpose"] == "PICKUP")
    planned_open = [x_ for x_ in planned if not run or still_todo(x_[0])]
    if todo(roster) == sorted(planned_open):
        with db.tx() as c:   # nothing that changes the route — just refresh the fingerprint
            c.execute("UPDATE route_plans SET signature=? WHERE id=?", (sig, act["id"]))
        return None
    d = draft_plan(date, bus_id, trip_no)
    if d and d["signature"] == sig:
        return d
    reasons = _diff_reason(p, roster, lines)
    pid = create_draft(date, bus_id, trip_no, user, role, "Approved change: " + reasons)
    return q1("SELECT * FROM route_plans WHERE id=?", (pid,))


def _diff_reason(p, roster, lines):
    planned = {st_["student_id"]: s["stop_id"] for s in p["stops"] if s["stop_id"] != SCHOOL_STOP for st_ in s["board"] + s["exit"]}
    names = {r["student_id"]: r["name"] for r in roster}
    out = []
    for r in roster:
        sid_ = r["student_id"]
        if r["status"] in ("ABSENT_DECLARED", "NO_SHOW", "CANCELLED") and sid_ in planned:
            out.append(f"{r['name']} {r['status'].replace('_', ' ').lower()}")
        elif r["status"] in ACTIVE_LINE and sid_ in planned and planned[sid_] != r["plan_stop"] and r["plan_stop"] != SCHOOL_STOP:
            out.append(f"{r['name']} stop changed")
        elif r["status"] in ACTIVE_LINE and sid_ not in planned:
            out.append(f"{r['name']} added")
    return ", ".join(out[:4]) + (" …" if len(out) > 4 else "") if out else "roster changed"
