# Factory Traffic Management System (Backend Developer Intern Assessment V2)

A resilient, event-driven traffic light control system for crossroads within a garment manufacturing facility. Built with Python 3.12, FastAPI, SQLite, and Vanilla HTML/CSS/JavaScript.

---

## 1. Architecture & Major Architectural Decisions

The application strictly decouples domain logic, application coordination, persistence, and external communication:

```
[REST API Clients / Dashboard]
              │
  FastAPI Application Layer (backend/adapters/http)
              │
  Serialized Per-Junction Worker / Actor (backend/application)
              │
  Traffic Engine Pure Domain Logic (backend/domain)
      ├── 2-Layer Safety Guard (Safe-list validation)
      ├── Priority & Aging Starvation Scheduler
      └── Emergency Preemption & Lease Manager
              │
  Ports & Adapters
      ├── SQLite Transactional Repository (backend/adapters/persistence)
      └── REST Simulator Controller Port (backend/adapters/controller)
```

### Major Architectural Decisions:
1. **Pure Domain Logic Engine:** The engine is deterministic and pure: `decide(state, input, config, now) -> (newState, actions)`. It does not read system clocks, query databases, or make network calls. All timestamps and configurations are passed in, allowing instant time-travel unit testing with zero flaky delays.
2. **Actor Pattern / Serialized Inbox:** Each junction possesses an independent `asyncio.Queue` inbox and a single worker. All events (vehicle sensors, admin commands, hardware replies, and clock ticks) serialize through this inbox. This completely prevents race conditions without complex distributed locks.
3. **Transactional Write-Through (Commit Before Send):** The in-memory state is backed by SQLite (`migrations/001_init.sql`). On each input, the state mutation, audit records, and controller commands are committed in a single atomic transaction. Controller commands are dispatched only after successful database commit.
4. **Desired vs. Actual Confirmed State:** Sending a signal command does not assume hardware execution. Dwell timers (min green, max green, yellow, all-red) only count once confirmation (`ACK`) is received from physical controllers.
5. **Two-Layer Safety Guard:** 
   - *Layer 1 (State by Design):* State holds at most one active green phase.
   - *Layer 2 (Safe-List Guard):* Every command emitted to the controller is validated against a strict allowed-transitions safe-list. Conflicting greens are prevented by software domain invariants and supported by the assumed hardware conflict monitor.
6. **Triple-Tier Failure Model:**
   - `FAILURE`: Controller offline or unacknowledged command exhausts retries (drives fail-safe to confirmed ALL-RED).
   - `DEGRADED`: Sensor offline; disables scoring scheduler and runs safe fixed-time round-robin cycles.
   - `WARNING`: Glitches (e.g. unknown vehicle type, cleared without arrival) logged without halting traffic.
7. **Dashboard Refresh Choice (Polling):** Polling every $1\,\text{s}$ was selected over WebSockets because it is simple, stateless, automatically recovers after temporary server or network dropouts, and avoids socket connection leaks.

---

## 2. Traffic-Control Algorithm

The automatic traffic scheduling evaluates queues, vehicle weights, waiting duration, and starvation prevention:

### Priority Weighting & Scoring Formula
For each waiting vehicle:
$$\text{Vehicle Score} = \text{weight}(\text{type}) + \text{aging\_weight\_per\_second} \times \text{seconds\_waited}$$

- **Weights (Configurable):**
  - `TRUCK`: 3.0
  - `FORKLIFT`: 2.0
  - `EMPLOYEE_VEHICLE`: 1.0
  - `UNKNOWN`: 0.5
  - `EMERGENCY`: Bypasses scoring via immediate preemption.
- **Aging Rate:** $0.05$ score gained per second waited. A low-priority employee vehicle waiting $60\,\text{s}$ scores $1.0 + (0.05 \times 60) = 4.0$, surpassing a newly arrived truck ($3.0$).

### Starvation Hard Cap
Any vehicle in a conflicting phase waiting longer than `max_wait_seconds` ($120\,\text{s}$) triggers a scheduled switch to its phase once minimum green elapses, preventing starvation under continuous high-priority traffic.

### Phase Selection Procedure
1. If emergency vehicle waiting on another phase $\rightarrow$ switch after emergency minimum green ($3\,\text{s}$).
2. If green dwell $< \text{green\_min\_seconds}$ ($10\,\text{s}$) $\rightarrow$ remain green.
3. If starved vehicle in conflicting phase $\rightarrow$ switch to starved phase.
4. If current phase has zero demand and other phase has traffic $\rightarrow$ switch.
5. If best challenger phase score $> \text{current phase score} \times 1.25$ (`switch_margin_ratio`) $\rightarrow$ switch.
6. If $\text{green dwell} \ge \text{green\_max\_seconds}$ ($30\,\text{s}$) and any vehicle waiting elsewhere $\rightarrow$ switch.
7. If no traffic waiting elsewhere $\rightarrow$ hold green (avoids pointless switching).

---

## 3. Traffic-State Transitions

Transitions follow a strict sequence:
$$\text{GREEN}(P) \longrightarrow \text{YELLOW}(P) \longrightarrow \text{ALL\_RED} \longrightarrow \text{GREEN}(Q)$$

- **Yellow Dwell:** Exactly $5\,\text{s}$ (`yellow_seconds`), counted from controller confirmation. Never shortened, including during startup and failure recovery (`RECOVERY_YELLOW` dwells for the full $5\,\text{s}$ after ACK before commanding `RED`).
- **All-Red Clearance:** Exactly $2\,\text{s}$ (`all_red_seconds`), counted from controller confirmation. Never shortened.
- **Confirmation Rule:** A new green command is sent only after all conflicting directions have confirmed `RED` for at least `all_red_seconds`.
- **Target Selection at ALL_RED End:**
  $$\text{Emergency Target} > \text{Manual Target} > \text{Starved Phase} > \text{Scored Phase} > \text{Round-Robin}$$
  *(Empty junctions after boot transition from ALL_RED to the first phase GREEN and never remain stuck in ALL_RED).*
- **State-Refresh Heartbeat:** While dwelling on confirmed GREEN, the engine periodically re-emits signal commands every $30\,\text{s}$ (configurable) to verify controller synchronization and avoid hardware drift.
- **Manual Lease on Restart:** If a manual lease has not expired when the system restarts (`now < expires_at`), it is preserved and suspended during initial boot recovery, then resumes automatically once stable ALL-RED is confirmed. If it expired during downtime, it is dropped.

---

## 4. Setup & Running Instructions

### Prerequisites
- Python 3.12+
- SQLite3
- Optional: `jq` for CLI formatting

### 1. Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run Automated Test Suite
To run all 34 automated tests across all test suites:
```bash
pytest tests/ -v
```

### 3. Launch Backend & Dashboard

#### Standard Production Configuration:
```bash
python3 -m backend.main
```

#### Demo Fast-Timer Configuration (Recommended for quick evaluation):
```bash
python3 -m backend.main --config config/demo.yaml
```
*(Uses short timers: $10\,\text{s}$ green, $2\,\text{s}$ yellow, $1\,\text{s}$ all-red, $3\,\text{s}$ recovery stability, and `enforce_timestamp_window: false` so you can paste the PDF's 2026-10-05 sample payloads without 400 stale errors).*

The server starts at `http://localhost:8000`.

- **Web Dashboard & Evaluator Simulator:** Open [http://localhost:8000/](http://localhost:8000/) in your browser.
- **Interactive Swagger API Docs:** Open [http://localhost:8000/docs](http://localhost:8000/docs).
- **OpenAPI JSON Spec:** Exported at `docs/openapi.json`.
- **Postman Collection:** Exported at `docs/postman_collection.json` (Collection v2.1 importable directly into Postman).
- **Database Schema:** Located at `migrations/001_init.sql`.

> [!NOTE]
> **Startup Recovery Period:** After starting or restarting, the junction drives physical lights to confirmed RED and verifies $10\,\text{s}$ of hardware stability ($3\,\text{s}$ in demo config) followed by ALL-RED clearance before serving the first green. Manual requests issued during this boot recovery receive HTTP 409 Conflict. Wait for the green light before testing manual overrides.

---

## 5. Instructions for Demonstrating the 9 Assessment Scenarios

All scenarios can be demonstrated directly from the **Evaluator Simulator Panel** on the web dashboard or via terminal `curl` commands.

### Scenario 1: Normal Traffic
1. Open dashboard at `http://localhost:8000/`.
2. In the **Simulator Panel**, select `NORTH` and `EMPLOYEE_VEHICLE`, click **Simulate Arrival** (3 times).
3. Select `EAST` and `TRUCK`, click **Simulate Arrival** (1 time).
4. Select `WEST` and `FORKLIFT`, click **Simulate Arrival** (2 times).
5. **Expected Result:** Queues display `NORTH: 3, EAST: 1, WEST: 2`. The scheduler evaluates scores, initiates yellow clearance ($5\,\text{s}$), all-red ($2\,\text{s}$), and serves the highest-scoring phase safely.

### Scenario 2: Priority Traffic vs. Aging
1. Send 2 `EMPLOYEE_VEHICLE` arrivals to `NORTH`.
2. Send 1 `TRUCK` arrival to `EAST`.
3. Notice that `EAST_WEST` gains scheduling priority due to truck weight ($3.0$).
4. As employee vehicles wait, their score climbs by $0.05/\text{s}$. Eventually, aging guarantees that `NORTH_SOUTH` wins and clears without starvation.

### Scenario 3: Emergency Preemption
1. While `NORTH_SOUTH` is confirmed `GREEN`, submit an arrival on `EAST` with vehicle type `EMERGENCY`:
```bash
curl -X POST http://localhost:8000/api/sensor-events \
  -H "Content-Type: application/json" \
  -d '{"event_id":"em-demo-1","junction_id":"A","direction":"EAST","event_type":"VEHICLE_ARRIVED","vehicle_id":"AMB-101","vehicle_type":"EMERGENCY","sequence_no":2001,"timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
```
2. **Expected Result:** Dashboard turns red with the **EMERGENCY PREEMPTION ACTIVE** banner. The active phase transitions safely: Yellow ($5\,\text{s}$), All-Red ($2\,\text{s}$), then `EAST_WEST` Green. Two conflicting phases are never green simultaneously.
3. Submit clearance for `AMB-101` $\rightarrow$ system automatically returns to `AUTOMATIC` control.

### Scenario 4: Manual Override
1. Wait until boot recovery completes and the junction shows GREEN.
2. Click **Give WEST Green** in the Manual Override panel.
3. **Expected Result:** Manual override banner displays with a $120\,\text{s}$ lease countdown. The junction transitions safely to `EAST_WEST` Green and holds it.
4. Click **Return to Automatic Control** $\rightarrow$ lease ends and normal automatic scheduling resumes.

### Scenario 5: Duplicate Event (Idempotency)
1. Submit an arrival event:
```bash
curl -X POST http://localhost:8000/api/sensor-events \
  -H "Content-Type: application/json" \
  -d '{"event_id":"dup-test-01","junction_id":"A","direction":"NORTH","event_type":"VEHICLE_ARRIVED","vehicle_id":"VH-DUP","vehicle_type":"TRUCK","sequence_no":3001,"timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
```
2. Submit the **exact same command a second time**.
3. **Expected Result:** Both requests return HTTP 200. The second response returns `"outcome": "DUPLICATE_IGNORED"`. Queue count increments exactly once.

### Scenario 6: Vehicle Clearance Without Arrival
1. Submit a `VEHICLE_CLEARED` event for an unseen vehicle ID:
```bash
curl -X POST http://localhost:8000/api/sensor-events \
  -H "Content-Type: application/json" \
  -d '{"event_id":"clr-ghost","junction_id":"A","direction":"SOUTH","event_type":"VEHICLE_CLEARED","vehicle_id":"GHOST-9","sequence_no":4001,"timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
```
2. **Expected Result:** Returns HTTP 200 with `"outcome": "ACCEPTED_NO_CHANGE"`. Queue count remains at 0 (never negative). A tombstone record is stored to reject any late out-of-order arrivals.

### Scenario 7: Controller Failure & Fail-Safe Recovery
1. In the simulator panel, set Controller Response Mode to `DROP` (silently drops commands).
2. Request a phase change or manual override.
3. **Expected Result:** Controller fails to ACK after 2 retries ($6\,\text{s}$). The junction enters `FAILURE` mode and drives lights to safe confirmed `ALL_RED` via yellow dwell ($5\,\text{s}$).
4. Set Response Mode back to `AUTO_ACK` and submit a controller `ONLINE` event.
5. **Expected Result:** System verifies confirmed ALL-RED, begins a $10\,\text{s}$ stability timer, and automatically returns to healthy operation.

### Scenario 8: Application Restart Recovery
1. Add vehicles and create an active manual lease.
2. Stop the backend server (`Ctrl+C`).
3. Restart the backend (`python3 -m backend.main`).
4. **Expected Result:** The server reloads persisted vehicle queues, processed event IDs, and audit history. Physical signals reset to `UNKNOWN` and run the fail-safe recovery sequence before resuming traffic. Mid-transition pending commands are abandoned, valid manual leases resume after recovery (or drop if expired during downtime), and emergencies older than $120\,\text{s}$ expire (downtime counts).

### Scenario 9: Concurrent Events Sequence
Execute the included parallel demo script:
```bash
./demo_09.sh
```
Or run the automated test suite:
- `tests/test_application.py`: Exercises 4 of the 5 concurrent events without hardware ACK.
- `tests/test_critical_review.py::test_critical_07_concurrency`: Exercises 50 parallel POSTs for idempotency and the full concurrent sequence including ACK handling.

All concurrent requests are safely serialized through the junction inbox actor, preventing race conditions.

---

## 6. Assumptions / Questions / Requirement Issues

This section documents engineering assumptions, ambiguities, and requirement decisions categorized across the six dimensions specified in Assessment Section 19:
- **Unclear:** Items 3, 5, 13, 14
- **Contradictory:** Items 2, 11
- **Unsafe:** Items 7, 12
- **Missing:** Items 4, 6, 8, 9, 10, 15, 18
- **Technically problematic:** Item 16
- **Dependent on a business decision:** Items 1, 17

| # | Issue Identified | Category | Engineering Decision & Rationale |
|---|---|---|---|
| **1** | Yellow before green? | Business Decision | **Not supported.** Yellow strictly means "stop soon". Using yellow for "prepare to go" creates forklift collision hazards in factories. |
| **2** | Immediate emergency preemption vs. transition sequence | Contradictory | **Preemption begins immediately, but transitions are never bypassed.** Green dwell reduces to emergency min ($3\,\text{s}$), followed by full yellow ($5\,\text{s}$) and all-red ($2\,\text{s}$). Yellow is never shortened, even during fail-safe recovery. |
| **3** | Vehicle types in scenario vs. event schema | Unclear | Mapped "delivery trucks" and "material carriers" to `TRUCK`, and "employee transport" to `EMPLOYEE_VEHICLE`. |
| **4** | `VEHICLE_CLEARED` example lacks `vehicle_type` | Missing | Made `vehicle_type` optional on clearance events; vehicle type is retrieved from the stored arrival record. |
| **5** | Authority of sequence numbers vs. event IDs | Unclear | `event_id` is authoritative for message deduplication. `sequence_no` is authoritative for event ordering per vehicle. |
| **6** | Authoritative timestamp | Missing | Server time is authoritative for timeouts, waiting times, and aging. Sensor timestamp is used only for sanity checks and audit records. |
| **7** | Negative queue hazard | Unsafe | Queues are computed as the count of individual waiting vehicles rather than integer decrement. Negative queues are impossible. |
| **8** | Emergency vs. Manual override | Missing | **Emergency always overrides manual control.** Manual commands submitted during emergency are rejected with HTTP 409 Conflict. Arrival of an emergency vehicle immediately terminates any active manual lease. |
| **9** | Manual override duration & disconnects | Missing | Implemented a renewable $120\,\text{s}$ lease. REST clients cannot detect disconnects, so lease expiry handles abandoned sessions. Unexpired leases resume after restart recovery; expired leases drop. |
| **10** | Competing emergencies on conflicting phases | Missing | Oldest waiting emergency is served first. If both sides have emergencies, a $45\,\text{s}$ hold cap forces alternation. |
| **11** | Stale emergencies: Runtime failure vs. Restart downtime | Contradictory | **Resolved distinction:** During active runtime `FAILURE` (online transient disruption), emergency expiration is paused because signals cannot turn green and vehicles cannot pass. However, during server restart / offline downtime (Spec 18.6), downtime counts: emergencies older than $120\,\text{s}$ from arrival expire upon restart recovery, as the vehicle has long since cleared or rerouted while the system was offline. |
| **12** | Restarting mid-transition | Unsafe | Physical hardware state cannot be assumed. All actual states reset to `UNKNOWN`, pending commands become `ABANDONED`, and the engine executes a fail-safe recovery sequence before traffic resumes. |
| **13** | Controller failure vs. Sensor failure severity | Unclear | Controller failures enter `FAILURE` (all-red stop). Sensor failures enter `DEGRADED` (safe fixed-time cycling). Retry timeout marks direction state `UNKNOWN` and enters `FAILURE`; controller `OFFLINE` status is set only from explicit status events. |
| **14** | Duplicate event HTTP status code | Unclear | Returns HTTP 200 with `outcome: DUPLICATE_IGNORED` to prevent well-behaved IoT sensors from endlessly retrying. |
| **15** | Empty junction boot target (P1) | Missing | After boot recovery and ALL-RED dwell, scheduler selects round-robin / first phase so junction reaches GREEN and never sits in ALL-RED. |
| **16** | Past sample timestamps in PDF (P4) | Technically Problematic | Added `enforce_timestamp_window: false` in `config/demo.yaml` so evaluators can test historical PDF payloads. |
| **17** | Flashing Red fallback | Business Decision | Real junctions fall back to flashing red. Since only RED, YELLOW, GREEN exist, ALL-RED is our safest software fail-safe. |
| **18** | Heartbeat state-refresh | Spec 15.8 | While idle on confirmed green, signal commands are re-transmitted every $30\,\text{s}$ to ensure controller alignment and detect unannounced reboots. |

---

## 7. Known Limits & Incomplete Features (What I Would Do Next)

### Known Limits
1. **Single-Process Model:** Designed for a single server process per junction. A clustered deployment would require external leader election or distributed actor coordination.
2. **Authentication:** Manual control endpoint accepts an unverified `requested_by` parameter. Production systems would require JWT / mTLS authentication.
3. **Hardware Assumption:** Software guards assume physical signal controllers include hardware conflict monitors (interlocks).
4. **Events during Downtime:** Sensor events arriving while the server is down are rejected unless the IoT gateway implements retries.

### What I Would Implement Next
1. **MQTT Adapter:** Implement an MQTT client port matching the existing `ControllerPort` interface so physical brokers (Mosquitto/EMQX) plug in without domain changes.
2. **Server-Sent Events (SSE):** Push live signal state changes to the dashboard to complement or replace $1\,\text{s}$ polling.
3. **Flashing Yellow / Flashing Red hardware signals:** Support 4-state controllers.
4. **Pedestrian / Turn-Phase expansion:** Extend junction configuration to support protected turn movements.

---

## 8. Automated Test Summary (34 Passing Tests)

All 34 automated tests across 5 test suites pass cleanly:

```bash
$ pytest tests/ -v
============================== 34 passed in 1.13s ==============================
```

| Suite | File | Tests | Key Invariants Covered |
|---|---|---|---|
| **Critical Tests (Review)** | `tests/test_critical_review.py` | 8 | 1. Stronger property test (350 steps, GREEN only after confirmed red $\ge 2\,\text{s}$, RED only after confirmed yellow $\ge 5\,\text{s}$, reachability of GREEN/YELLOW/ALL-RED)<br>2. CLEARED before ARRIVED (seq 1502 then 1501 ignored, queue stays 0, no ghost vehicle)<br>3. Dwell boundaries (4.99s vs 5.0s yellow, 1.99s vs 2.0s all-red, 9.99s vs 10.0s min green, time before ACK ignored)<br>4. Controller replies (duplicate ACK ignored, late ACK accepted, wrong-state/NACK enters failure, unknown command ID ignored)<br>5. Manual control (lease renewal, expiry, return to auto, emergency ending lease)<br>6. Restart recovery (duplicate event ID ignored, pending commands abandoned, valid lease resumes, expired lease dropped, stale emergency dropped)<br>7. Concurrency (50 parallel POSTs with identical event ID yield exactly 1 ACCEPTED and 49 DUPLICATE_IGNORED)<br>8. API error status codes (malformed body returns 400, unknown junction returns 404, manual command during emergency returns 409) |
| **Domain Core & State Engine** | `tests/test_domain_engine.py` | 16 | Layer 2 safety guard, Table 8.4 main rows, boot recovery reaching green (P1), emergency vs normal min green (P2), emergency retention across failure (P3), degraded cycling (P4), aging & starvation cap, 500-step property test |
| **HTTP REST API** | `tests/test_api.py` | 6 | Sensor event idempotency, manual override workflow, simulator controller behavior, audit history pagination |
| **Persistence & Transactions** | `tests/test_persistence.py` | 3 | Atomic transactions, crash-recovery reload, audit log cursor pagination |
| **Concurrency & Actor Inbox** | `tests/test_application.py` | 1 | Scenario 9 concurrent event sequence execution |
| **Total** | | **34** | **All 34 tests passing (100% pass rate; not a code coverage figure)** |

---

## 9. AI / Tool Usage

- **Claude (Anthropic):** Used for initial specification drafting, state transition modeling, requirement contradiction analysis, and architectural review.
- **Antigravity AI (Google DeepMind):** Used as an intelligent pair-programming coding assistant for implementation, test authoring, and edge-case validation.



