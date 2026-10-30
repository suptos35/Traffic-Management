from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from backend.domain.types import (
    AuditRecord,
    AuditSeverity,
    CommandStatus,
    ControllerCommand,
    DeviceStatus,
    DirectionState,
    EmergencyState,
    EventOutcome,
    FailureState,
    JunctionState,
    ManualState,
    Mode,
    ProcessedEventRecord,
    SignalColor,
    StepKind,
    StepState,
    StepStatus,
    VehicleRecord,
    VehicleStatus,
    VehicleType,
)


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SQLiteRepository:
    """Synchronous SQLite repository for transactional junction state, commands, and audit records."""

    def __init__(self, db_path: str = "traffic.db"):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        migration_file = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"
        if not migration_file.exists():
            # Alternative fallback if running from root
            migration_file = Path("migrations/001_init.sql")

        with self.get_connection() as conn:
            with open(migration_file, "r", encoding="utf-8") as f:
                conn.executescript(f.read())

    def save_junction_transaction(
        self,
        state: JunctionState,
        audit_records: List[AuditRecord],
        new_commands: List[ControllerCommand],
    ) -> None:
        """Saves junction state, direction states, vehicles, processed events, commands, and audit records in ONE atomic transaction."""
        conn = self.get_connection()
        try:
            conn.execute("BEGIN TRANSACTION;")

            # 1. Save junction
            definition_json = json.dumps({
                "directions": list(state.directions.keys()),
                "phases": [{"name": p, "directions": dirs} for p, dirs in state.phases.items()],
            })

            conn.execute(
                """
                INSERT INTO junctions (
                    junction_id, definition_json, step_kind, step_phase, step_status,
                    step_started_at, step_confirmed_at, next_target,
                    manual_active, manual_target_phase, manual_expires_at, manual_requested_by, manual_suspended,
                    emergency_active, emergency_serving_phase, emergency_hold_started_at,
                    failure_active, failure_since, failure_reason, failure_recovery_stable_since,
                    command_counter, transition_counter, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(junction_id) DO UPDATE SET
                    definition_json = excluded.definition_json,
                    step_kind = excluded.step_kind,
                    step_phase = excluded.step_phase,
                    step_status = excluded.step_status,
                    step_started_at = excluded.step_started_at,
                    step_confirmed_at = excluded.step_confirmed_at,
                    next_target = excluded.next_target,
                    manual_active = excluded.manual_active,
                    manual_target_phase = excluded.manual_target_phase,
                    manual_expires_at = excluded.manual_expires_at,
                    manual_requested_by = excluded.manual_requested_by,
                    manual_suspended = excluded.manual_suspended,
                    emergency_active = excluded.emergency_active,
                    emergency_serving_phase = excluded.emergency_serving_phase,
                    emergency_hold_started_at = excluded.emergency_hold_started_at,
                    failure_active = excluded.failure_active,
                    failure_since = excluded.failure_since,
                    failure_reason = excluded.failure_reason,
                    failure_recovery_stable_since = excluded.failure_recovery_stable_since,
                    command_counter = excluded.command_counter,
                    transition_counter = excluded.transition_counter,
                    updated_at = excluded.updated_at;
                """,
                (
                    state.junction_id,
                    definition_json,
                    state.step.kind.value,
                    state.step.phase,
                    state.step.status.value,
                    _dt_to_iso(state.step.started_at),
                    _dt_to_iso(state.step.confirmed_at),
                    state.next_target,
                    1 if state.manual.active else 0,
                    state.manual.target_phase,
                    _dt_to_iso(state.manual.expires_at),
                    state.manual.requested_by,
                    1 if state.manual.suspended else 0,
                    1 if state.emergency.active else 0,
                    state.emergency.serving_phase,
                    _dt_to_iso(state.emergency.hold_started_at),
                    1 if state.failure.active else 0,
                    _dt_to_iso(state.failure.since),
                    state.failure.reason,
                    _dt_to_iso(state.failure.recovery_stable_since),
                    state.command_counter,
                    state.transition_counter,
                    _dt_to_iso(state.updated_at or datetime.now(timezone.utc)),
                ),
            )

            # 2. Save directions
            for d_name, d_state in state.directions.items():
                pending_cmd_id = d_state.pending_command.command_id if d_state.pending_command else None
                conn.execute(
                    """
                    INSERT INTO junction_directions (
                        junction_id, direction, desired, actual, pending_command_id,
                        last_confirmed_at, signal_controller_status, sensor_status, last_status_timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(junction_id, direction) DO UPDATE SET
                        desired = excluded.desired,
                        actual = excluded.actual,
                        pending_command_id = excluded.pending_command_id,
                        last_confirmed_at = excluded.last_confirmed_at,
                        signal_controller_status = excluded.signal_controller_status,
                        sensor_status = excluded.sensor_status,
                        last_status_timestamp = excluded.last_status_timestamp;
                    """,
                    (
                        state.junction_id,
                        d_name,
                        d_state.desired.value,
                        d_state.actual.value,
                        pending_cmd_id,
                        _dt_to_iso(d_state.last_confirmed_at),
                        d_state.signal_controller_status.value,
                        d_state.sensor_status.value,
                        _dt_to_iso(d_state.last_status_timestamp),
                    ),
                )

            # 3. Save vehicles
            for v in state.vehicles.values():
                conn.execute(
                    """
                    INSERT INTO vehicles (
                        junction_id, vehicle_id, vehicle_type, direction, status,
                        arrived_at_server, arrived_at_sensor, last_sequence_no
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(junction_id, vehicle_id) DO UPDATE SET
                        vehicle_type = excluded.vehicle_type,
                        direction = excluded.direction,
                        status = excluded.status,
                        arrived_at_server = excluded.arrived_at_server,
                        arrived_at_sensor = excluded.arrived_at_sensor,
                        last_sequence_no = excluded.last_sequence_no;
                    """,
                    (
                        state.junction_id,
                        v.vehicle_id,
                        v.vehicle_type.value,
                        v.direction,
                        v.status.value,
                        _dt_to_iso(v.arrived_at_server),
                        _dt_to_iso(v.arrived_at_sensor),
                        v.last_sequence_no,
                    ),
                )

            # 4. Save processed events
            for p_ev in state.processed_events.values():
                outcome_str = p_ev.outcome.value if hasattr(p_ev.outcome, "value") else str(p_ev.outcome)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_events (
                        junction_id, event_id, received_at, outcome, payload_hash
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    (
                        state.junction_id,
                        p_ev.event_id,
                        _dt_to_iso(p_ev.received_at),
                        outcome_str,
                        p_ev.payload_hash,
                    ),
                )

            # 5. Save new commands
            for cmd in new_commands:
                conn.execute(
                    """
                    INSERT INTO commands (
                        command_id, junction_id, direction, requested_state, attempt, sent_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(command_id) DO UPDATE SET
                        attempt = excluded.attempt,
                        status = excluded.status;
                    """,
                    (
                        cmd.command_id,
                        cmd.junction_id,
                        cmd.direction,
                        cmd.requested_state.value,
                        cmd.attempt,
                        _dt_to_iso(cmd.sent_at),
                        cmd.status.value,
                    ),
                )

            # 6. Save audit records
            for r in audit_records:
                cursor = conn.execute(
                    """
                    INSERT INTO audit_log (
                        junction_id, timestamp, event_type, severity, direction,
                        previous_state, new_state, command_id, cause_event_id,
                        transition_id, vehicle_id, mode, reason, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        r.junction_id,
                        _dt_to_iso(r.timestamp),
                        r.event_type,
                        r.severity.value,
                        r.direction,
                        r.previous_state.value if r.previous_state else None,
                        r.new_state.value if r.new_state else None,
                        r.command_id,
                        r.cause_event_id,
                        r.transition_id,
                        r.vehicle_id,
                        r.mode.value if r.mode else None,
                        r.reason,
                        json.dumps(r.details or {}),
                    ),
                )
                r.id = cursor.lastrowid

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def load_junction_state(self, junction_id: str) -> Optional[JunctionState]:
        """Loads full junction state from the database at startup."""
        conn = self.get_connection()
        try:
            row = conn.execute("SELECT * FROM junctions WHERE junction_id = ?;", (junction_id,)).fetchone()
            if not row:
                return None

            definition = json.loads(row["definition_json"])
            phases = {p["name"]: p["directions"] for p in definition["phases"]}

            # Step
            step = StepState(
                kind=StepKind(row["step_kind"]),
                phase=row["step_phase"],
                status=StepStatus(row["step_status"]),
                started_at=_iso_to_dt(row["step_started_at"]),
                confirmed_at=_iso_to_dt(row["step_confirmed_at"]),
            )

            # Directions
            dir_rows = conn.execute("SELECT * FROM junction_directions WHERE junction_id = ?;", (junction_id,)).fetchall()
            directions: Dict[str, DirectionState] = {}
            for dr in dir_rows:
                pending_cmd = None
                if dr["pending_command_id"]:
                    cmd_row = conn.execute(
                        "SELECT * FROM commands WHERE command_id = ?;", (dr["pending_command_id"],)
                    ).fetchone()
                    if cmd_row:
                        pending_cmd = ControllerCommand(
                            command_id=cmd_row["command_id"],
                            junction_id=cmd_row["junction_id"],
                            direction=cmd_row["direction"],
                            requested_state=SignalColor(cmd_row["requested_state"]),
                            attempt=cmd_row["attempt"],
                            sent_at=_iso_to_dt(cmd_row["sent_at"]),
                            status=CommandStatus(cmd_row["status"]),
                        )

                directions[dr["direction"]] = DirectionState(
                    desired=SignalColor(dr["desired"]),
                    actual=SignalColor(dr["actual"]),
                    pending_command=pending_cmd,
                    last_confirmed_at=_iso_to_dt(dr["last_confirmed_at"]),
                    signal_controller_status=DeviceStatus(dr["signal_controller_status"]),
                    sensor_status=DeviceStatus(dr["sensor_status"]),
                    last_status_timestamp=_iso_to_dt(dr["last_status_timestamp"]),
                )

            # Vehicles
            veh_rows = conn.execute("SELECT * FROM vehicles WHERE junction_id = ?;", (junction_id,)).fetchall()
            vehicles: Dict[str, VehicleRecord] = {}
            for vr in veh_rows:
                vehicles[vr["vehicle_id"]] = VehicleRecord(
                    vehicle_id=vr["vehicle_id"],
                    vehicle_type=VehicleType(vr["vehicle_type"]),
                    direction=vr["direction"],
                    status=VehicleStatus(vr["status"]),
                    arrived_at_server=_iso_to_dt(vr["arrived_at_server"]),
                    arrived_at_sensor=_iso_to_dt(vr["arrived_at_sensor"]),
                    last_sequence_no=vr["last_sequence_no"],
                )

            # Processed events
            pe_rows = conn.execute("SELECT * FROM processed_events WHERE junction_id = ?;", (junction_id,)).fetchall()
            processed_events: Dict[str, ProcessedEventRecord] = {}
            for pr in pe_rows:
                raw_outcome = pr["outcome"]
                try:
                    outcome_enum = EventOutcome(raw_outcome)
                except (ValueError, TypeError):
                    outcome_enum = EventOutcome.ACCEPTED

                processed_events[pr["event_id"]] = ProcessedEventRecord(
                    event_id=pr["event_id"],
                    received_at=_iso_to_dt(pr["received_at"]),
                    outcome=outcome_enum,
                    payload_hash=pr["payload_hash"],
                )

            manual = ManualState(
                active=bool(row["manual_active"]),
                target_phase=row["manual_target_phase"],
                expires_at=_iso_to_dt(row["manual_expires_at"]),
                requested_by=row["manual_requested_by"],
                suspended=bool(row["manual_suspended"]),
            )

            emergency = EmergencyState(
                active=bool(row["emergency_active"]),
                serving_phase=row["emergency_serving_phase"],
                hold_started_at=_iso_to_dt(row["emergency_hold_started_at"]),
            )

            failure = FailureState(
                active=bool(row["failure_active"]),
                since=_iso_to_dt(row["failure_since"]),
                reason=row["failure_reason"],
                recovery_stable_since=_iso_to_dt(row["failure_recovery_stable_since"]),
            )

            return JunctionState(
                junction_id=junction_id,
                step=step,
                next_target=row["next_target"],
                directions=directions,
                phases=phases,
                vehicles=vehicles,
                processed_events=processed_events,
                manual=manual,
                emergency=emergency,
                failure=failure,
                command_counter=row["command_counter"],
                transition_counter=row["transition_counter"],
                updated_at=_iso_to_dt(row["updated_at"]),
            )
        finally:
            conn.close()

    def list_junction_ids(self) -> List[str]:
        conn = self.get_connection()
        try:
            rows = conn.execute("SELECT junction_id FROM junctions ORDER BY junction_id ASC;").fetchall()
            return [r["junction_id"] for r in rows]
        finally:
            conn.close()

    def query_audit_log(
        self,
        junction_id: str,
        limit: int = 50,
        before_id: Optional[int] = None,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Queries audit log ordered by id DESC with cursor-based paging."""
        conn = self.get_connection()
        try:
            query = "SELECT * FROM audit_log WHERE junction_id = ?"
            params: List[Any] = [junction_id]

            if before_id is not None:
                query += " AND id < ?"
                params.append(before_id)

            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)

            if severity:
                query += " AND severity = ?"
                params.append(severity)

            if since:
                query += " AND timestamp >= ?"
                params.append(_dt_to_iso(since))

            query += " ORDER BY id DESC LIMIT ?;"
            params.append(limit + 1)

            rows = conn.execute(query, params).fetchall()

            has_more = len(rows) > limit
            result_rows = rows[:limit]

            items = []
            for r in result_rows:
                items.append({
                    "id": r["id"],
                    "junction_id": r["junction_id"],
                    "timestamp": r["timestamp"],
                    "event_type": r["event_type"],
                    "severity": r["severity"],
                    "direction": r["direction"],
                    "previous_state": r["previous_state"],
                    "new_state": r["new_state"],
                    "command_id": r["command_id"],
                    "cause_event_id": r["cause_event_id"],
                    "transition_id": r["transition_id"],
                    "vehicle_id": r["vehicle_id"],
                    "mode": r["mode"],
                    "reason": r["reason"],
                    "details": json.loads(r["details_json"]),
                })

            next_cursor = result_rows[-1]["id"] if has_more and result_rows else None
            return items, next_cursor
        finally:
            conn.close()

    def housekeeping_cleanup(
        self,
        retention_days: int,
        remember_seconds: float,
        now: datetime,
    ) -> None:
        """Section 18.9: Cleanup old tombstones, event IDs, and audit records."""
        audit_cutoff = _dt_to_iso(now - timedelta(days=retention_days))
        event_cutoff = _dt_to_iso(now - timedelta(seconds=remember_seconds))

        conn = self.get_connection()
        try:
            conn.execute("BEGIN TRANSACTION;")
            conn.execute("DELETE FROM audit_log WHERE timestamp < ?;", (audit_cutoff,))
            conn.execute("DELETE FROM processed_events WHERE received_at < ?;", (event_cutoff,))
            conn.execute(
                "DELETE FROM vehicles WHERE status = 'CLEARED' AND arrived_at_server < ?;",
                (event_cutoff,),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()
