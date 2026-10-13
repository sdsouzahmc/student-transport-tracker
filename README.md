# Student Transport Tracker — v2.0

**Student Transport Tracker v2.0** builds on School Bus Tracker v1.0, which stays available unchanged for demonstrations. It includes:

- the crew app for the bus tablet,
- the office portal,
- the school receiving-staff screen,
- the parent portal, in English and Arabic.

## What is new in v2.0 — Student Location Registration & Dynamic Trip Planning

- **Student locations** (office menu): each student has a default pickup and drop-off location (address, map pin, notes, linked guardian). The drop-off can be the same as the pickup or a separate place. Changes, from the office or from parents, need school approval, and every version is kept. Missing or invalid pins, and pins with no approved safe stop within 600 m, are flagged.
- **Trip planning** (office menu): builds each trip from the day's eligible roster (schedule, batch, group and purpose, with absences and cancellations removed). Students are grouped onto approved **safe stops** (a home pin is never a stop). Stops are ordered by estimated road time. The planner checks bus capacity at every stop, the school deadline and ride times, and an infeasible plan cannot be activated. Shows the map, the ordered stop list (boarding and exiting, planned and actual arrival, status), each student's registered location next to the scan GPS, and the full plan history with approver, times and reasons.
- **Replanning during the trip**: an approved change (absence, stop or bus change, location change) produces a revised draft for the remaining stops only. Completed stops stay in the history. The destination of a child who is on board is never removed. A supervisor or the transport manager must approve the draft before it goes live, and approval notifies the crew and the affected parents.
- **Combined Trip 2**: Batch 1 Junior drop-off and Batch 2 pickup are planned as one sequence, with occupancy tracked after every stop.
- **Crew and driver app**: the stops follow the approved route, with planned times. The driver's NEXT STOP screen offers Waze and Google Maps navigation and updates itself, so the driver never has to touch the app while driving. The last approved route is stored encrypted on the device and used when the device is offline, and scans are queued for sync as before.

Built with **Streamlit** (Python) and **SQLite**.

On first start it loads your **School_Bus_Trip_Dataset.xlsx** (in `data/`): 120 students, 3 buses and 75 trips with labelled test cases. You can switch to a generated full-size set (1,250 students, 28 buses) in *Setup & data*.

> **About the sample dates:** the dataset covers 4–8 Oct 2026. If you run the demo before those dates, the import moves the sample back by whole weeks so it ends before today and the weekdays stay the same. After 8 Oct, nothing moves. You can turn this off in *Setup & data → Data source*.

---

## 1. Run it on Windows

1. Install **Python 3.10 or newer** from https://www.python.org/downloads/. During setup, tick **"Add Python to PATH"**.
2. Unzip this folder, for example to `C:\SchoolBusTracker`.
3. Double-click **`run_demo.bat`**. The first run installs the required packages, which takes 2–3 minutes.
4. The browser opens at **http://localhost:8501**.

To run it from a terminal instead: `pip install -r requirements.txt`, then `streamlit run app.py`.

To start again with fresh data, close the window and delete `school_bus.db` and the `device_store` folder. Default student locations are created automatically from the dataset.

## 2. Demo logins

| Role | Login | PIN | What they see |
|---|---|---|---|
| Administrator | `admin` | `admin123` | Everything. Sign-in asks for a second-step code, which is shown on screen in the demo. |
| Transport manager | `manager` | `manager123` | Office portal and setup. Also has the second step. |
| Dispatcher | `dispatcher` | `1234` | Live trips, lists, exceptions, notices, reports |
| Supervisor / care-taker | `sup01`, `care01` (bus 1) … | `1234` | Crew app for their own bus |
| Driver | `drv01` … | `1234` | Crew app, view only. Drivers do not scan. |
| School receiving staff | `recv1` | `1234` | Confirms children arriving at school |
| Parent / guardian | `g001` … `g120` | `1234` | Their own children only |

## 3. Suggested 15-minute demo

1. **Office view (`admin`).** Open *Live trips*: the map, open incidents, and the dataset's open custody case (Student 120, BUS003). Then open *Reports*. There are 30 views, filtered by date, trip, batch, group and bus. Select a row to drill down, and download CSV or PDF.
2. **Daily bus lists.** This is the list each bus receives for each trip, with absences and approved changes already applied.
3. **Parent (`g001`, second browser window).** Press **"My child is not coming today"**. Go back to *Daily bus lists*: the child now shows as *Declared absent*.
4. **Crew (`sup01`, on a phone or a third window).** The flow:
   1. *Today's list*.
   2. Confirm the crew and start **Trip 2**. This trip has three movements: Batch 1 Junior drop-off, then Batch 2 Junior and Senior pickup.
   3. Scan or type badge codes, in the **Check In** direction, at **School**.
   4. Things to show: a duplicate scan, an invalid badge, a child from another bus (*wrong bus → awaiting transfer*), and manual entry (needs a reason and a second person).
   5. Choose a stop and switch to **Check Out**. The app asks **who received the child** and accepts only authorized recipients. Choosing *No approved recipient* refuses the release and raises a custody incident.
   6. Open *Device: connection & GPS* and turn on **Offline mode**. Scan a few children; they are saved on the device, encrypted. Turn offline off and press **Sync**.
   7. Try **Close trip**. It is blocked while any child is on board, still expected, or awaiting transfer. The person who did the physical sweep must be named.
5. **Office: *Exceptions & approvals*.** Approve the transfer, then work an incident: assign it, escalate it, log a call, resolve it.
6. **School receipt (`recv1`).** Children arriving on pickup trips are accepted by a named staff member.
7. **Setup & data.** Badges can be issued, revoked and replaced; a revoked badge is then rejected at scan. Also here: ID cards with QR codes, validated CSV import, device revocation, users, backup, and demo tools to simulate a full day or live trips.

Ready-to-use badge codes are in *Setup → Badges & ID cards*. The ID-card PDF prints QR codes that the crew app camera reads.

## 4. On a phone

The camera works only on **HTTPS** pages or `localhost`.

- **Streamlit Community Cloud (free).** Push this folder to GitHub. At https://share.streamlit.io, choose *New app*, select the repository and `app.py`, then *Deploy*. You get an `https://…streamlit.app` link. Data resets whenever the app restarts, which is fine for a demo.
- **Same Wi-Fi, without the camera.** Run `streamlit run app.py --server.address 0.0.0.0` and open `http://<PC-IP>:8501` on the phone. Use the *Badge reader / type code* or *Manual entry* tabs instead of the camera.

## 5. How the spec maps to this demo

See **COMPLIANCE.md** for the section-by-section checklist. It says what is fully working, what is simulated in the demo, and what is a production step.

## 6. Files

```
app.py                  login (+ second step for admin/manager), language, role-based navigation
core/db.py              schema, generated full-size data, helpers
core/engine.py          rules: manifests, check-in/out, handover, no-show, wrong bus/stop, capacity, closure,
                        offline sync, absences, change requests, corrections, notifications, simulator
core/dataset_import.py  validated import of School_Bus_Trip_Dataset.xlsx (same layout = your future data feed)
core/reports.py         30 report views, CSV / PDF export, contingency roster PDF
core/device_store.py    encrypted offline queue and roster (survive a restart)
core/i18n.py            English / Arabic
views/crew.py           bus tablet app        views/receipt.py   school receiving staff
views/parent.py         parent portal         views/live.py      live trips + map
views/lists.py          daily bus lists       views/exceptions.py incidents, transfers, changes, corrections
views/notices.py        parent notices        views/reports.py   reports
views/setup.py          setup, import, badges, devices, users, backup, demo tools
data/School_Bus_Trip_Dataset.xlsx
```
