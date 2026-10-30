-- Initial schema for Factory Traffic Management System
-- SQLite Dialect

CREATE TABLE IF NOT EXISTS junctions (
    junction_id TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL,
    step_kind TEXT NOT NULL,
    step_phase TEXT,
    step_status TEXT NOT NULL,
    step_started_at TEXT NOT NULL,
    step_confirmed_at TEXT,
    next_target TEXT,
    manual_active INTEGER NOT NULL DEFAULT 0,
    manual_target_phase TEXT,
    manual_expires_at TEXT,
    manual_requested_by TEXT,
    manual_suspended INTEGER NOT NULL DEFAULT 0,
    emergency_active INTEGER NOT NULL DEFAULT 0,
    emergency_serving_phase TEXT,
    emergency_hold_started_at TEXT,
    failure_active INTEGER NOT NULL DEFAULT 0,
    failure_since TEXT,
    failure_reason TEXT,
    failure_recovery_stable_since TEXT,
    command_counter INTEGER NOT NULL DEFAULT 0,
    transition_counter INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS junction_directions (
    junction_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    desired TEXT NOT NULL,
    actual TEXT NOT NULL,
    pending_command_id TEXT,
    last_confirmed_at TEXT,
    signal_controller_status TEXT NOT NULL,
    sensor_status TEXT NOT NULL,
    last_status_timestamp TEXT,
    PRIMARY KEY (junction_id, direction),
    FOREIGN KEY (junction_id) REFERENCES junctions (junction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vehicles (
    junction_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    vehicle_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    arrived_at_server TEXT NOT NULL,
    arrived_at_sensor TEXT NOT NULL,
    last_sequence_no INTEGER NOT NULL,
    PRIMARY KEY (junction_id, vehicle_id),
    FOREIGN KEY (junction_id) REFERENCES junctions (junction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS processed_events (
    junction_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (junction_id, event_id),
    FOREIGN KEY (junction_id) REFERENCES junctions (junction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    junction_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    requested_state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (junction_id) REFERENCES junctions (junction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    junction_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    direction TEXT,
    previous_state TEXT,
    new_state TEXT,
    command_id TEXT,
    cause_event_id TEXT,
    transition_id TEXT,
    vehicle_id TEXT,
    mode TEXT,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_junction_id_id ON audit_log (junction_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_vehicles_waiting ON vehicles (junction_id, status);
