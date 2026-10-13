"""SQLite data layer: schema, connection helpers, demo seed data and audit log."""
import hashlib
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SBT_DB", os.path.join(BASE, "school_bus.db"))
TZ = ZoneInfo("Asia/Qatar")

SCHOOL_NAME = "Shantiniketan Indian School"
SCHOOL_LAT, SCHOOL_LON = 25.3052, 51.4676
SCHOOL_STOP = 0           # pseudo stop id used for "at school"
TRANSPORT_PHONE = "+974 4400 0000"

# The five daily trips (spec §1). Movement = (batch, group, purpose)
TRIPS = {
    1: {"window": ("05:30", "06:45"), "movements": [(1, "Junior", "PICKUP"), (1, "Senior", "PICKUP")],
        "label": "Batch 1 Junior & Senior pickup"},
    2: {"window": ("10:30", "12:15"), "movements": [(1, "Junior", "DROPOFF"), (2, "Junior", "PICKUP"), (2, "Senior", "PICKUP")],
        "label": "Batch 1 Junior drop-off + Batch 2 Junior & Senior pickup"},
    3: {"window": ("13:45", "14:30"), "movements": [(1, "Senior", "DROPOFF")], "label": "Batch 1 Senior drop-off"},
    4: {"window": ("14:30", "15:30"), "movements": [(2, "Junior", "DROPOFF")], "label": "Batch 2 Junior drop-off"},
    5: {"window": ("17:00", "18:00"), "movements": [(2, "Senior", "DROPOFF")], "label": "Batch 2 Senior drop-off"},
}

ROLES = ["admin", "manager", "dispatcher", "driver", "supervisor", "caretaker", "receiving", "guardian"]
MFA_ROLES = {"admin", "manager"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (id INTEGER PRIMARY KEY, name TEXT, role TEXT, phone TEXT, bus_id INTEGER);
CREATE TABLE IF NOT EXISTS buses (
  id INTEGER PRIMARY KEY, bus_no TEXT UNIQUE, plate TEXT, capacity INTEGER, fvts_vehicle TEXT, route_name TEXT,
  driver_id INTEGER, supervisor_id INTEGER, caretaker_id INTEGER);
CREATE TABLE IF NOT EXISTS stops (id INTEGER PRIMARY KEY, bus_id INTEGER, seq INTEGER, name TEXT, area TEXT, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS guardians (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, relation TEXT, verified INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, class_name TEXT, section TEXT, batch INTEGER, grp TEXT,
  bus_id INTEGER, stop_id INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS student_guardians (student_id INTEGER, guardian_id INTEGER, is_primary INTEGER, PRIMARY KEY(student_id, guardian_id));
CREATE TABLE IF NOT EXISTS recipients (id INTEGER PRIMARY KEY, student_id INTEGER, name TEXT, relation TEXT, phone TEXT, id_number TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS emergency_contacts (id INTEGER PRIMARY KEY, student_id INTEGER, name TEXT, relation TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS badges (id INTEGER PRIMARY KEY, code TEXT UNIQUE, student_id INTEGER, status TEXT, issued_at TEXT,
  revoked_at TEXT, reason TEXT, replaced_by TEXT);
CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, name TEXT, bus_id INTEGER, status TEXT, registered_at TEXT, revoked_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, pin_hash TEXT, role TEXT, staff_id INTEGER, guardian_id INTEGER,
  display_name TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS absences (id INTEGER PRIMARY KEY, student_id INTEGER, date TEXT, trip_no INTEGER, reason TEXT,
  declared_by TEXT, source TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS change_requests (id INTEGER PRIMARY KEY, student_id INTEGER, date TEXT, trip_no INTEGER, kind TEXT,
  new_stop_id INTEGER, new_bus_id INTEGER, recipient_name TEXT, recipient_phone TEXT, note TEXT, requested_by TEXT,
  status TEXT, decided_by TEXT, decided_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS trip_runs (
  id INTEGER PRIMARY KEY, bus_id INTEGER, date TEXT, trip_no INTEGER, status TEXT,
  start_ts TEXT, start_lat REAL, start_lon REAL, end_ts TEXT, end_lat REAL, end_lon REAL,
  driver_id INTEGER, supervisor_id INTEGER, caretaker_id INTEGER,
  final_driver_id INTEGER, final_supervisor_id INTEGER, final_caretaker_id INTEGER,
  sweep_by TEXT, sweep_ts TEXT, started_by TEXT, closed_by TEXT, device_id INTEGER, cancelled INTEGER DEFAULT 0,
  UNIQUE(bus_id, date, trip_no));
CREATE TABLE IF NOT EXISTS manifest (
  id INTEGER PRIMARY KEY, run_id INTEGER, student_id INTEGER, purpose TEXT, batch INTEGER, grp TEXT, stop_id INTEGER, status TEXT,
  entry_ts TEXT, entry_lat REAL, entry_lon REAL, exit_ts TEXT, exit_lat REAL, exit_lon REAL,
  handover TEXT, recipient_name TEXT, recipient_relation TEXT, received_by TEXT, note TEXT,
  UNIQUE(run_id, student_id));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, client_uuid TEXT UNIQUE, run_id INTEGER, manifest_id INTEGER, student_id INTEGER, badge_code TEXT,
  kind TEXT, result TEXT, reason TEXT, captured_ts TEXT, received_ts TEXT, lat REAL, lon REAL, accuracy REAL, gps_ok INTEGER,
  stop_id INTEGER, operator TEXT, device_id INTEGER, method TEXT, manual_reason TEXT, verified_by TEXT,
  sync_state TEXT, clock_flag TEXT, connectivity TEXT DEFAULT 'Online', device_seq INTEGER);
CREATE TABLE IF NOT EXISTS corrections (id INTEGER PRIMARY KEY, event_id INTEGER, manifest_id INTEGER, field TEXT, old_value TEXT,
  new_value TEXT, reason TEXT, requested_by TEXT, status TEXT, decided_by TEXT, decided_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, ts TEXT, run_id INTEGER, bus_id INTEGER, student_id INTEGER, kind TEXT,
  severity TEXT, details TEXT, status TEXT, assigned_to TEXT, resolution TEXT, resolved_ts TEXT, receiving_custody TEXT, ack_ts TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS incident_contacts (id INTEGER PRIMARY KEY, incident_id INTEGER, ts TEXT, contact_name TEXT, phone TEXT,
  outcome TEXT, by_user TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, ts TEXT, guardian_id INTEGER, student_id INTEGER, run_id INTEGER,
  channel TEXT, kind TEXT, message TEXT, status TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, delayed_offline INTEGER DEFAULT 0,
  is_read INTEGER DEFAULT 0, sent_ts TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts TEXT, username TEXT, role TEXT, action TEXT, entity TEXT, entity_id TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS ix_man_run ON manifest(run_id);
CREATE INDEX IF NOT EXISTS ix_man_stu ON manifest(student_id);
CREATE INDEX IF NOT EXISTS ix_ev_run ON events(run_id);
CREATE INDEX IF NOT EXISTS ix_ev_stu ON events(student_id);
CREATE INDEX IF NOT EXISTS ix_run_date ON trip_runs(date, trip_no);
CREATE INDEX IF NOT EXISTS ix_not_g ON notifications(guardian_id);
CREATE INDEX IF NOT EXISTS ix_abs ON absences(student_id, date);
"""


def now():
    return datetime.now(TZ).replace(tzinfo=None)


def ts(dt=None):
    if isinstance(dt, str):
        return dt
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def today():
    return now().strftime("%Y-%m-%d")


def connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextmanager
def tx(con=None):
    """Use an existing connection (no commit) or open one and commit at the end."""
    if con is not None:
        yield con
        return
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def q(sql, params=(), con=None):
    with tx(con) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def q1(sql, params=(), con=None):
    r = q(sql, params, con)
    return r[0] if r else None


def x(sql, params=(), con=None):
    with tx(con) as c:
        return c.execute(sql, params).lastrowid


def pin_hash(pin):
    return hashlib.sha256(("sbt-demo-salt:" + str(pin)).encode()).hexdigest()


def audit(username, role, action, entity="", entity_id="", details="", con=None):
    x("INSERT INTO audit(ts, username, role, action, entity, entity_id, details) VALUES (?,?,?,?,?,?,?)",
      (ts(), username, role, action, entity, str(entity_id), details), con)


# ------------------------------------------------------------------ seed
FIRST_M = ["Ahmed", "Mohammed", "Omar", "Yusuf", "Ali", "Hamad", "Khalid", "Abdullah", "Faisal", "Saad", "Ibrahim", "Hassan", "Aarav",
           "Arjun", "Rohan", "Vihaan", "Aditya", "Rayan", "Zayd", "Adam", "Liam", "Noah", "Ethan", "Lucas", "Daniel", "Joseph", "Ryan",
           "Karim", "Tariq", "Bilal", "Hamza", "Imran", "Kevin", "Mark", "John", "Paul"]
FIRST_F = ["Fatima", "Mariam", "Aisha", "Noor", "Sara", "Layla", "Hessa", "Reem", "Dana", "Lulwa", "Shaikha", "Ananya", "Diya",
           "Priya", "Sana", "Zara", "Hiba", "Emma", "Olivia", "Sophia", "Mia", "Ava", "Grace", "Hannah", "Leah", "Amira", "Yasmin",
           "Salma", "Rania", "Huda", "Angel", "Joy", "Maria", "Anna"]
LAST = ["Al-Thani", "Al-Kuwari", "Al-Mannai", "Al-Sulaiti", "Al-Marri", "Al-Hajri", "Al-Kaabi", "Al-Naimi", "Al-Mohannadi", "Khan",
        "Sheikh", "Hussain", "Rahman", "Qureshi", "Malik", "Nair", "Menon", "Pillai", "Kumar", "Sharma", "Patel", "Iyer", "Reddy",
        "Fernandes", "D'Souza", "Santos", "Reyes", "Cruz", "Garcia", "Smith", "Brown", "Wilson", "Haddad", "Nasser", "Mansour",
        "Saleh", "Aziz", "Farouk", "Youssef", "Abdallah"]
STAFF_NAMES = ["Ramesh Kumar", "Suresh Babu", "Anil Thomas", "Joseph Mathew", "Imran Ali", "Shahid Khan", "Rajan Pillai", "Biju Varghese",
               "Faisal Ahmed", "Naveed Iqbal", "Santosh Rai", "Dinesh Thapa", "Ravi Shankar", "Manoj Nair", "Sajid Hussain",
               "Bashir Ahmed", "Prakash Gurung", "Jose Fernandes", "Arjun Das", "Kamal Hossain", "Rafiq Uddin", "Salim Malik",
               "Vinod Kumar", "Tariq Mehmood", "George Mathew", "Hari Prasad", "Abdul Kareem", "Noel D'Souza", "Mary Joseph",
               "Fatima Begum", "Sheeba Thomas", "Aisha Khan", "Rosemary Dsouza", "Nisha Nair", "Lina Haddad", "Asma Qureshi",
               "Bindu Menon", "Grace Fernandes", "Hanan Saleh", "Jessy George", "Shabana Ali", "Reena Pillai", "Samira Nasser",
               "Teresa Cruz", "Anita Sharma", "Mona Aziz", "Divya Iyer", "Huda Mansour", "Rekha Patel", "Salma Rahman",
               "Angela Reyes", "Noura Farouk", "Priya Kumar", "Joyce Santos", "Ruby Mathew", "Leena Das"]
AREAS = [("Al Sadd", 25.2850, 51.5050), ("West Bay", 25.3230, 51.5280), ("Al Waab", 25.2620, 51.4700), ("Al Rayyan", 25.2920, 51.4240),
         ("Old Airport", 25.2520, 51.5530), ("Al Wakrah", 25.1710, 51.6030), ("Lusail", 25.4200, 51.4900), ("The Pearl", 25.3710, 51.5510),
         ("Madinat Khalifa", 25.3180, 51.4780), ("Al Gharrafa", 25.3350, 51.4560), ("Abu Hamour", 25.2400, 51.5000),
         ("Al Hilal", 25.2580, 51.5480), ("Najma", 25.2710, 51.5460), ("Muaither", 25.2900, 51.3950), ("Al Thumama", 25.2300, 51.5400),
         ("Duhail", 25.3480, 51.4760), ("Mansoura", 25.2700, 51.5400), ("Fereej Bin Mahmoud", 25.2830, 51.5200),
         ("Ain Khaled", 25.2300, 51.4700), ("Al Aziziya", 25.2500, 51.4600), ("Umm Salal", 25.4100, 51.4050), ("Al Luqta", 25.3200, 51.4500)]
CLASSES = ["KG1", "KG2", "Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5", "Grade 6", "Grade 7", "Grade 8", "Grade 9", "Grade 10",
           "Grade 11", "Grade 12"]
JUNIOR = set(CLASSES[:7])


def init_db(reset=False, n_buses=28, n_students=1250, seed=11, seed_data=True):
    if reset:
        store = os.path.join(os.path.dirname(DB_PATH) or ".", "device_store")
        if os.path.isdir(store):
            for f in os.listdir(store):
                os.remove(os.path.join(store, f))
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(DB_PATH + ext)
            except FileNotFoundError:
                pass
    con = connect()
    try:
        con.executescript(SCHEMA)
        _POS.clear()
        if con.execute("SELECT COUNT(*) FROM buses").fetchone()[0] or not seed_data:
            return
        con.execute("INSERT OR REPLACE INTO settings VALUES ('data_source', 'Generated full-size demo (1,250 students, 28 buses)')")
        r = random.Random(seed)
        names = STAFF_NAMES[:]
        r.shuffle(names)

        def staff(name, role, phone, bus_id=None):
            return con.execute("INSERT INTO staff(name, role, phone, bus_id) VALUES (?,?,?,?)", (name, role, phone, bus_id)).lastrowid

        def user(u, pin, role, display, staff_id=None, guardian_id=None):
            con.execute("INSERT INTO users(username, pin_hash, role, staff_id, guardian_id, display_name) VALUES (?,?,?,?,?,?)",
                        (u, pin_hash(pin), role, staff_id, guardian_id, display))

        def phone():
            return f"+974 {r.choice('3567')}{r.randint(100, 999)} {r.randint(1000, 9999)}"

        stops_by_bus = {}
        for b in range(1, n_buses + 1):
            plate = str(r.randint(100000, 999999))
            bid = con.execute("INSERT INTO buses(bus_no, plate, capacity, fvts_vehicle, route_name) VALUES (?,?,?,?,?)",
                              (f"BUS-{b:02d}", plate, 40, f"{plate} BUS-{b:02d}", "")).lastrowid
            d = staff(f"{r.choice(FIRST_M)} {r.choice(LAST)}", "driver", phone(), bid)
            s = staff(names[(b * 2) % len(names)], "supervisor", phone(), bid)
            c = staff(names[(b * 2 + 1) % len(names)], "caretaker", phone(), bid)
            areas = r.sample(AREAS, 2)
            con.execute("UPDATE buses SET driver_id=?, supervisor_id=?, caretaker_id=?, route_name=? WHERE id=?",
                        (d, s, c, f"Route {b:02d} – {areas[0][0]} / {areas[1][0]}", bid))
            ids = []
            for k in range(r.randint(6, 9)):
                a, lat, lon = areas[k % 2]
                ids.append(con.execute("INSERT INTO stops(bus_id, seq, name, area, lat, lon) VALUES (?,?,?,?,?,?)",
                                       (bid, k + 1, f"{a} – Stop {k + 1}", a, lat + r.uniform(-.009, .009), lon + r.uniform(-.009, .009))).lastrowid)
            stops_by_bus[bid] = ids
            con.execute("INSERT INTO devices(name, bus_id, status, registered_at) VALUES (?,?,?,?)", (f"BUS-{b:02d} Tablet", bid, "ACTIVE", ts()))
            user(f"drv{b:02d}", "1234", "driver", con.execute("SELECT name FROM staff WHERE id=?", (d,)).fetchone()[0], d)
            user(f"sup{b:02d}", "1234", "supervisor", con.execute("SELECT name FROM staff WHERE id=?", (s,)).fetchone()[0], s)
            user(f"care{b:02d}", "1234", "caretaker", con.execute("SELECT name FROM staff WHERE id=?", (c,)).fetchone()[0], c)
        for k in range(1, 7):
            sid = staff(f"{r.choice(FIRST_F)} {r.choice(LAST)}", "receiving", phone())
            user(f"recv{k}", "1234", "receiving", con.execute("SELECT name FROM staff WHERE id=?", (sid,)).fetchone()[0], sid)
        mid = staff("Khalid Al-Marri", "manager", phone())
        did = staff("Sanjay Menon", "dispatcher", phone())
        aid = staff("System Administrator", "admin", phone())
        user("admin", "admin123", "admin", "System Administrator", aid)
        user("manager", "manager123", "manager", "Khalid Al-Marri (Transport Manager)", mid)
        user("dispatcher", "1234", "dispatcher", "Sanjay Menon (Dispatcher)", did)

        # families, students, recipients, badges
        bus_ids = list(stops_by_bus)
        load = {b: 0 for b in bus_ids}
        n = 0
        g_count = 0
        while n < n_students:
            last = r.choice(LAST)
            father = con.execute("INSERT INTO guardians(name, phone, email, relation, verified) VALUES (?,?,?,?,1)",
                                 (f"{r.choice(FIRST_M)} {last}", phone(), f"parent{g_count + 1}@example.com", "Father")).lastrowid
            g_count += 1
            mother = None
            if r.random() < 0.55:
                mother = con.execute("INSERT INTO guardians(name, phone, email, relation, verified) VALUES (?,?,?,?,1)",
                                     (f"{r.choice(FIRST_F)} {last}", phone(), f"parent{g_count + 1}@example.com", "Mother")).lastrowid
                g_count += 1
            kids = 1 if r.random() < 0.7 else 2
            bus_id = min(bus_ids, key=lambda b: (load[b], r.random()))   # keep buses balanced
            load[bus_id] += kids
            stop_id = r.choice(stops_by_bus[bus_id])
            nanny = f"{r.choice(FIRST_F)} {r.choice(LAST)}" if r.random() < 0.3 else None
            for _ in range(kids):
                if n >= n_students:
                    break
                n += 1
                girl = r.random() < 0.5
                cls = r.choice(CLASSES)
                grp = "Junior" if cls in JUNIOR else "Senior"
                batch = 1 if r.random() < 0.5 else 2
                sid = con.execute("INSERT INTO students(code, name, class_name, section, batch, grp, bus_id, stop_id) VALUES (?,?,?,?,?,?,?,?)",
                                  (f"STU{n:04d}", f"{r.choice(FIRST_F if girl else FIRST_M)} {last}", cls, r.choice("ABCD"), batch, grp,
                                   bus_id, stop_id)).lastrowid
                con.execute("INSERT INTO student_guardians VALUES (?,?,1)", (sid, father))
                if mother:
                    con.execute("INSERT INTO student_guardians VALUES (?,?,0)", (sid, mother))
                # authorized recipients (~2% have none -> demo of 'no approved recipient = no release')
                if r.random() > 0.02:
                    for gid in [father] + ([mother] if mother else []):
                        gname, gph, grel = con.execute("SELECT name, phone, relation FROM guardians WHERE id=?", (gid,)).fetchone()
                        con.execute("INSERT INTO recipients(student_id, name, relation, phone, id_number) VALUES (?,?,?,?,?)",
                                    (sid, gname, grel, gph, f"QID {r.randint(28000000000, 29999999999)}"))
                    if nanny:
                        con.execute("INSERT INTO recipients(student_id, name, relation, phone, id_number) VALUES (?,?,?,?,?)",
                                    (sid, nanny, "Nanny", phone(), f"QID {r.randint(28000000000, 29999999999)}"))
                con.execute("INSERT INTO emergency_contacts(student_id, name, relation, phone) VALUES (?,?,?,?)",
                            (sid, f"{r.choice(FIRST_M)} {last}", "Uncle", phone()))
                code = f"BDG-{n:04d}-{r.randint(1000, 9999)}"
                if r.random() < 0.02:   # some replaced badges: old one revoked
                    old = f"BDG-{n:04d}-{r.randint(1000, 9999)}"
                    con.execute("INSERT INTO badges(code, student_id, status, issued_at, revoked_at, reason, replaced_by) VALUES (?,?,?,?,?,?,?)",
                                (old, sid, "REVOKED", ts(now() - timedelta(days=60)), ts(now() - timedelta(days=10)), "Lost card", code))
                con.execute("INSERT INTO badges(code, student_id, status, issued_at) VALUES (?,?,?,?)", (code, sid, "ACTIVE", ts(now() - timedelta(days=60))))
        # guardian logins for the first 40 guardians that have children (demo)
        for i, g in enumerate(con.execute("""SELECT DISTINCT g.id, g.name FROM guardians g JOIN student_guardians sg ON sg.guardian_id=g.id
                                             ORDER BY g.id LIMIT 40""").fetchall(), start=1):
            user(f"g{i:03d}", "1234", "guardian", g[1], None, g[0])
        con.commit()
    finally:
        con.close()
    if seed_data:              # v2.0: default pickup / drop-off locations for the generated students
        import planner
        planner.seed_locations(force=True)


# ------------------------------------------------------------------ common lookups
def bus(bus_id, con=None):
    return q1("SELECT * FROM buses WHERE id=?", (bus_id,), con)


def buses(con=None):
    return q("SELECT * FROM buses ORDER BY bus_no", (), con)


def staff_name(staff_id, con=None):
    r = q1("SELECT name FROM staff WHERE id=?", (staff_id,), con) if staff_id else None
    return r["name"] if r else ""


def stop(stop_id, con=None):
    if stop_id == SCHOOL_STOP:
        lat, lon = school_pos(con)
        return {"id": SCHOOL_STOP, "name": "School", "lat": lat, "lon": lon, "seq": 0}
    return q1("SELECT * FROM stops WHERE id=?", (stop_id,), con)


def setting(key, default=None, con=None):
    try:
        r = q1("SELECT value FROM settings WHERE key=?", (key,), con)
    except sqlite3.OperationalError:
        return default
    return r["value"] if r else default


def set_setting(key, value, con=None):
    x("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, str(value)), con)


_POS = {}


def school_pos(con=None):
    if DB_PATH not in _POS:
        _POS[DB_PATH] = (float(setting("school_lat", SCHOOL_LAT, con)), float(setting("school_lon", SCHOOL_LON, con)))
    return _POS[DB_PATH]


def stops_of_bus(bus_id, con=None):
    return q("SELECT * FROM stops WHERE bus_id=? ORDER BY seq", (bus_id,), con)


def get_user(username, pin):
    u = q1("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),))
    return u if u and u["pin_hash"] == pin_hash(pin.strip()) else None
