from datetime import datetime, timezone
import os
import tempfile
import pytest
from backend.adapters.persistence import SQLiteRepository
from backend.config.config import AppConfig
from backend.domain import (
    AuditRecord,
    AuditSeverity,
    ControllerCommand,
    DirectionState,
    Mode,
    SignalColor,
    StartupInput,
    StepKind,
    StepState,
    StepStatus,
    TrafficEngine,
    VehicleRecord,
    VehicleStatus,
    VehicleType,
    create_initial_junction_state,
)


@pytest.fixture
def temp_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = SQLiteRepository(db_path=path)
    yield repo
    if os.path.exists(path):
        os.remove(path)


def test_save_and_load_junction_snapshot(temp_repo: SQLiteRepository):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = create_initial_junction_state(
        "A", ["NORTH", "SOUTH", "EAST", "WEST"], {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}, now
    )
    state.vehicles["VH-100"] = VehicleRecord("VH-100", VehicleType.TRUCK, "NORTH", VehicleStatus.WAITING, now, now, 1)

    audit = AuditRecord(
        junction_id="A",
        timestamp=now,
        event_type="VEHICLE_DETECTED",
        severity=AuditSeverity.INFO,
        direction="NORTH",
        vehicle_id="VH-100",
        reason="Vehicle detected",
    )

    temp_repo.save_junction_transaction(state, [audit], [])

    # Load back
    loaded = temp_repo.load_junction_state("A")
    assert loaded is not None
    assert loaded.junction_id == "A"
    assert "VH-100" in loaded.vehicles
    assert loaded.vehicles["VH-100"].vehicle_type == VehicleType.TRUCK


def test_restart_recovery_workflow(temp_repo: SQLiteRepository):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    config = AppConfig.load_from_yaml("config/default_config.yaml").defaults

    # Create steady GREEN state before "crash"
    state = create_initial_junction_state(
        "A", ["NORTH", "SOUTH", "EAST", "WEST"], {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}, now
    )
    state.step = StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now)
    state.directions["NORTH"] = DirectionState(SignalColor.GREEN, SignalColor.GREEN)
    state.directions["SOUTH"] = DirectionState(SignalColor.GREEN, SignalColor.GREEN)
    state.directions["EAST"] = DirectionState(SignalColor.RED, SignalColor.RED)
    state.directions["WEST"] = DirectionState(SignalColor.RED, SignalColor.RED)

    temp_repo.save_junction_transaction(state, [], [])

    # Simulate reboot: load from DB and run StartupInput
    reboot_now = datetime(2026, 10, 30, 10, 5, 0, tzinfo=timezone.utc)
    loaded_state = temp_repo.load_junction_state("A")
    assert loaded_state is not None

    rebooted, actions, outcome, _ = TrafficEngine.decide(loaded_state, StartupInput(), config, reboot_now)
    assert outcome.value == "ACCEPTED"

    # All actual states must be UNKNOWN
    for d, d_state in rebooted.directions.items():
        assert d_state.actual == SignalColor.UNKNOWN

    # Must be in recovery
    assert rebooted.step.kind in (StepKind.RECOVERY_YELLOW, StepKind.RECOVERY_RED)


def test_audit_log_pagination(temp_repo: SQLiteRepository):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = create_initial_junction_state(
        "A", ["NORTH", "SOUTH", "EAST", "WEST"], {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}, now
    )

    records = [
        AuditRecord(
            junction_id="A",
            timestamp=now,
            event_type="TEST_EVENT",
            severity=AuditSeverity.INFO,
            reason=f"Event number {i}",
        )
        for i in range(15)
    ]

    temp_repo.save_junction_transaction(state, records, [])

    # Query page 1 (limit 10)
    items, next_cursor = temp_repo.query_audit_log("A", limit=10)
    assert len(items) == 10
    assert next_cursor is not None

    # Query page 2 using cursor
    items2, next_cursor2 = temp_repo.query_audit_log("A", limit=10, before_id=next_cursor)
    assert len(items2) == 5
    assert next_cursor2 is None
