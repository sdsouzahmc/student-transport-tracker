# Spec compliance — School_Bus_Tracker_One_Page_Summary

Legend: ✅ working in the demo · 🟡 working but simulated in the demo · 🔜 production step (not something a demo can show)

## §1 Scope
| Requirement | Status | Where |
|---|---|---|
| Five daily trips with the given windows and batch/group movements | ✅ | `core/db.py` TRIPS. Used everywhere: lists, manifests, punctuality, reports |
| Trip 2 keeps batch, group, purpose and scans separate for each child | ✅ | Every manifest line stores batch / group / PICKUP or DROPOFF. Batch/group report |
| Roles: admin, transport manager, dispatcher, driver, supervisor, care-taker, receiving staff, verified guardian | ✅ | Logins for every role. Menus by role (`app.py`) |
| Crew app (Android), office portal, parent portal | 🟡 | All three run as one web app that works on phones and tablets. Packaging it as an Android app is a production step |
| English and Arabic | ✅ | Language switch, right-to-left layout. Crew and parent screens are translated |

## §2 Setup
| Requirement | Status | Where |
|---|---|---|
| Students, classes, guardians, authorized recipients, emergency contacts | ✅ | Setup → Students |
| Buses, capacity, crew, routes, stops, schedules | ✅ | Setup → Buses & crew, Stops |
| Validated import | ✅ | Workbook import (all sheets cross-checked; nothing loads if there are errors). Students CSV import with a row-by-row check |
| Badges: issue, revoke, replace | ✅ | Setup → Badges. A revoked badge is rejected at scan and logged |
| Manifests by batch, group and movement | ✅ | Daily bus lists, crew *Today's list* |
| Apply absences and approved changes | ✅ | Parent "not coming today" and office-approved stop / bus / one-day-recipient changes update the list right away |
| Allocation conflict check | ✅ | Exceptions → Allocation check: missing bus or stop, no badge, no recipient, stop not on route, manifest over capacity, unverified guardian |

## §3 Trip workflow
| Requirement | Status | Where |
|---|---|---|
| Start: confirm crew; record start time and location | ✅ | Crew app → Start trip |
| Explicit Check In / Check Out | ✅ | Direction switch on every scan |
| Check-out verifies destination and handover and records the recipient | ✅ | Home release only to an authorized recipient, chosen by name. School check-out needs the named receiving staff member. Wrong stop is flagged |
| End: reconciliation, named physical sweep, final crew | ✅ | Close trip (blocked until reconciled) |

## §4 Student record
| Requirement | Status | Where |
|---|---|---|
| Trip, batch, group, purpose, stop, times, coordinates, GPS accuracy, operator, device, capture method, handover, status, sync state | ✅ | Reports → *Student journey record* |
| Prevent duplicates, invalid badges, exit without entry, routine overcapacity | ✅ | Rules engine. Overcapacity needs an override with a reason, which creates an incident |
| Flag wrong bus / wrong stop | ✅ | Wrong bus → *awaiting transfer approval* + incident. Wrong stop → flag + incident |
| Manual entry needs a reason and verification | ✅ | Manual tab: reason + second crew member |
| Keep original events alongside approved corrections | ✅ | Corrections are stored separately and approved by a second person. Scan events are never changed |

## §5 Safety
| Requirement | Status | Where |
|---|---|---|
| Block completion while any child is on board, unaccounted for, or awaiting a confirmed transfer | ✅ | Close trip; server re-checks after an offline sync |
| Distinguish absence / no-show / completed / transfer | ✅ | Separate statuses throughout (also returned to school, transport cancelled) |
| School receipt and authorized home release; no approved recipient = no release | ✅ | Release is refused, the child stays with the crew, then is checked out back to school staff. Custody incident + parent notice |
| Incidents, escalation, contact log | ✅ | Exceptions → Incidents: assign, escalate, log calls with outcome, resolve |

## §6 Offline
| Requirement | Status | Where |
|---|---|---|
| Encrypted rosters | ✅ | Crew → *Download encrypted roster* (Fernet, key per device) |
| Durable scans that survive a restart | ✅ | Offline queue is saved encrypted on disk (`device_store/`) |
| Sync without duplicates; expose conflicts | ✅ | Each scan has a unique ID, so a resend is ignored. Rejected items are marked CONFLICT (Reports → Offline & sync) |
| Keep capture and receipt times separately; flag clock problems, missing GPS, stale data | ✅ | Captured vs received on every event; clock flag; GPS-quality report; *data as of* on reports |
| A trip closed offline stays Pending Sync | ✅ | Closed on the device → Pending Sync until the server validates it after sync |
| Printable contingency roster | ✅ | PDF with tick boxes, per trip |
| Driver must not scan | ✅ | Driver login is view-only; the server also rejects driver scans |
| Real loss of network | 🟡 | Simulated with the *Offline mode* switch. A native app would detect it automatically |

## §7 Parents
| Requirement | Status | Where |
|---|---|---|
| Linked children only: status, accepted scans, handover, last location, update time | ✅ | Parent portal |
| Absence declaration ("my child is not coming today") and change requests | ✅ | One tap per child; the bus list updates at once |
| In-app and e-mail notices with retry and failure tracking | 🟡 | In-app is real. E-mail sending is simulated (about 6% failures; retry up to 3 times). Production: SMTP / e-mail service |
| Label delayed offline updates; urgent cases need a direct call | ✅ | 🕓 delayed label; phone banner on every parent page |

## §8 Reports
| Requirement | Status | Where |
|---|---|---|
| 30 MVP views | ✅ | Reports (numbered 1–30) |
| Filters by trip, batch, group (also date, bus), drill-down | ✅ | Filter bar; select a row → trip manifest or student journey |
| CSV, printable PDF, metric definitions, data freshness | ✅ | On every report |

## §9 Security & pilot
| Requirement | Status | Where |
|---|---|---|
| Permissions by role | ✅ | Menus and actions by role; parents see only linked children |
| MFA for admin and transport manager | 🟡 | Second-step code; the demo shows it on screen. Production: authenticator app / SMS |
| Encryption | 🟡 | Device data is encrypted; PINs are hashed. Production: HTTPS plus an encrypted database and backups |
| Device revocation | ✅ | Setup → Devices. A revoked tablet cannot start trips or submit scans |
| Audit | ✅ | Every login, trip, scan decision, approval and setup change. Reports → Audit log |
| Backups | 🟡 | Setup → Backup (manual download). Production: automatic nightly encrypted backups |
| Live GPS | 🔜 | The demo uses scan coordinates. Production: Autotrace / FVTS vehicle feed (each bus has an *Autotrace vehicle* field) |
| Pilot plan | 🔜 | Suggested: 2–3 buses for 2 weeks with paper roster in parallel → review the reports → all 28 buses |
