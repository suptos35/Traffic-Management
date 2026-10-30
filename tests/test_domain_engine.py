from datetime import datetime, timedelta, timezone
import pytest
from backend.config.config import AppConfig, DefaultsConfig
from backend.domain import (
    AdminCommandInput,
    AuditRecord,
    AuditEventType,
    AuditSeverity,
    CommandStatus,
    ControllerCommand,
    ControllerReplyInput,
    DeviceStatus,
    DeviceStatusInput,
    DeviceType,
    DirectionState,
    EventOutcome,
    JunctionState,
    Mode,
    SafetyGuard,
    SendSignalCommandAction,
    SensorEventInput,
    SignalColor,
    StartupInput,
    StepKind,
    StepState,
    StepStatus,
    TickInput,
    TrafficEngine,
    VehicleStatus,
    VehicleType,
    compute_mode,
    create_initial_junction_state,
)


@pytest.fixture
def config() -> DefaultsConfig:
    app_cfg = AppConfig.load_from_yaml("config/default_config.yaml")
    return app_cfg.defaults


@pytest.fixture
def initial_state() -> JunctionState:
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = ["NORTH", "SOUTH", "EAST", "WEST"]
    phases = {
        "NORTH_SOUTH": ["NORTH", "SOUTH"],
        "EAST_WEST": ["EAST", "WEST"],
    }
    return create_initial_junction_state("A", directions, phases, now)


def test_initial_state_recovery_red(initial_state: JunctionState, config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    assert initial_state.step.kind == StepKind.RECOVERY_RED
    assert initial_state.step.status == StepStatus.PENDING

    # Run startup
    state, actions, outcome, _ = TrafficEngine.decide(initial_state, StartupInput(), config, now)
    assert outcome == EventOutcome.ACCEPTED

    # Should have emitted RED or YELLOW commands for all directions since all are UNKNOWN
    commands = [a.command for a in actions if isinstance(a, SendSignalCommandAction)]
    assert len(commands) == 4
    for cmd in commands:
        assert cmd.requested_state == SignalColor.YELLOW


def test_safety_guard_blocks_conflicting_greens(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "SOUTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "EAST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
        "WEST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
    }

    # Trying to command EAST to GREEN while NORTH/SOUTH are GREEN must be blocked!
    cmd = ControllerCommand("cmd-1", "A", "EAST", SignalColor.GREEN, 1, now)
    is_safe, reason = SafetyGuard.validate_command(cmd, "EAST_WEST", phases, directions)
    assert not is_safe
    assert "not confirmed RED" in reason


def test_sensor_event_duplicate_and_idempotency(initial_state: JunctionState, config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    ev = SensorEventInput(
        event_id="evt-101",
        junction_id="A",
        direction="NORTH",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="VH-1",
        vehicle_type="TRUCK",
        sequence_no=1,
        timestamp=now,
    )

    state, actions, outcome, _ = TrafficEngine.decide(initial_state, ev, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert state.vehicles["VH-1"].status == VehicleStatus.WAITING

    # Submit exact same event_id again
    state2, actions2, outcome2, _ = TrafficEngine.decide(state, ev, config, now)
    assert outcome2 == EventOutcome.DUPLICATE_IGNORED


def test_cleared_without_arrival_never_negative_queue(initial_state: JunctionState, config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    ev = SensorEventInput(
        event_id="evt-102",
        junction_id="A",
        direction="EAST",
        event_type="VEHICLE_CLEARED",
        vehicle_id="VH-UNKNOWN",
        sequence_no=1,
        timestamp=now,
    )

    state, actions, outcome, _ = TrafficEngine.decide(initial_state, ev, config, now)
    assert outcome == EventOutcome.ACCEPTED_NO_CHANGE
    # Tombstone is stored, but vehicle is CLEARED, so queue count is 0
    waiting_count = sum(1 for v in state.vehicles.values() if v.direction == "EAST" and v.status == VehicleStatus.WAITING)
    assert waiting_count == 0


def test_out_of_order_sequence_ignored(initial_state: JunctionState, config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    ev1 = SensorEventInput(
        event_id="evt-103",
        junction_id="A",
        direction="NORTH",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="VH-2",
        vehicle_type="FORKLIFT",
        sequence_no=10,
        timestamp=now,
    )
    state, _, outcome1, _ = TrafficEngine.decide(initial_state, ev1, config, now)
    assert outcome1 == EventOutcome.ACCEPTED

    # Older sequence arrives later
    ev2 = SensorEventInput(
        event_id="evt-104",
        junction_id="A",
        direction="NORTH",
        event_type="VEHICLE_CLEARED",
        vehicle_id="VH-2",
        sequence_no=5,
        timestamp=now,
    )
    state, _, outcome2, _ = TrafficEngine.decide(state, ev2, config, now)
    assert outcome2 == EventOutcome.IGNORED_OUT_OF_ORDER
    # Vehicle should still be WAITING
    assert state.vehicles["VH-2"].status == VehicleStatus.WAITING


def test_emergency_preemption_full_sequence(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "SOUTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "EAST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
        "WEST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    # 1. Emergency vehicle arrives on EAST
    ev = SensorEventInput(
        event_id="em-1",
        junction_id="A",
        direction="EAST",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="AMB-1",
        vehicle_type="EMERGENCY",
        sequence_no=1,
        timestamp=now,
    )
    state, actions, outcome, _ = TrafficEngine.decide(state, ev, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert compute_mode(state) == Mode.EMERGENCY

    # 2. Advance time past green_min_emergency_seconds (3s)
    now_3s = datetime(2026, 10, 30, 10, 0, 4, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_3s)

    # State should now be transitioning to YELLOW on NORTH_SOUTH
    assert state.step.kind == StepKind.YELLOW
    assert state.step.phase == "NORTH_SOUTH"
    assert state.next_target == "EAST_WEST"

    # Controller confirms YELLOW
    for d in ["NORTH", "SOUTH"]:
        cmd_id = state.directions[d].pending_command.command_id
        reply = ControllerReplyInput(command_id=cmd_id, status="ACK", actual_state=SignalColor.YELLOW)
        state, _, _, _ = TrafficEngine.decide(state, reply, config, now_3s)

    assert state.step.status == StepStatus.CONFIRMED

    # 3. Advance past yellow_seconds (5s)
    now_8s = datetime(2026, 10, 30, 10, 0, 10, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_8s)

    # Should transition to ALL_RED
    assert state.step.kind == StepKind.ALL_RED

    # Controller confirms RED
    for d in ["NORTH", "SOUTH"]:
        cmd_id = state.directions[d].pending_command.command_id
        reply = ControllerReplyInput(command_id=cmd_id, status="ACK", actual_state=SignalColor.RED)
        state, _, _, _ = TrafficEngine.decide(state, reply, config, now_8s)

    assert state.step.status == StepStatus.CONFIRMED

    # 4. Advance past all_red_seconds (2s)
    now_11s = datetime(2026, 10, 30, 10, 0, 13, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_11s)

    # Should now transition to GREEN on EAST_WEST!
    assert state.step.kind == StepKind.GREEN
    assert state.step.phase == "EAST_WEST"
    green_cmds = [a.command for a in actions if isinstance(a, SendSignalCommandAction)]
    assert {c.direction for c in green_cmds} == {"EAST", "WEST"}


def test_manual_override_rejected_during_emergency(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions={
            "NORTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN),
            "SOUTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN),
            "EAST": DirectionState(SignalColor.RED, SignalColor.RED),
            "WEST": DirectionState(SignalColor.RED, SignalColor.RED),
        },
        phases={"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]},
        updated_at=now,
    )

    # Add emergency
    ev = SensorEventInput("em-2", "A", "EAST", "VEHICLE_ARRIVED", "AMB-2", 1, now, "EMERGENCY")
    state, _, _, _ = TrafficEngine.decide(state, ev, config, now)
    assert compute_mode(state) == Mode.EMERGENCY

    # Try manual request -> must be rejected with INVALID (mapped to 409)
    admin_cmd = AdminCommandInput(command="MANUAL_GREEN_REQUEST", direction="WEST", requested_by="admin")
    state, _, outcome, msg = TrafficEngine.decide(state, admin_cmd, config, now)
    assert outcome == EventOutcome.REJECTED_INVALID
    assert msg == "EMERGENCY_ACTIVE"


def test_controller_timeout_and_retries(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    cmd = ControllerCommand("cmd-A-1", "A", "NORTH", SignalColor.YELLOW, 1, now)
    directions = {
        "NORTH": DirectionState(SignalColor.YELLOW, SignalColor.GREEN, pending_command=cmd),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.YELLOW, "NORTH_SOUTH", StepStatus.PENDING, now),
        next_target=None,
        directions=directions,
        phases={"NORTH_SOUTH": ["NORTH", "SOUTH"]},
        updated_at=now,
    )

    # 1. Advance past ack_timeout_seconds (2s) -> Attempt 2 retry
    now_2s = datetime(2026, 10, 30, 10, 0, 3, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_2s)
    retries = [a.command for a in actions if isinstance(a, SendSignalCommandAction)]
    assert len(retries) == 1
    assert retries[0].attempt == 2
    assert state.directions["NORTH"].pending_command.attempt == 2

    # 2. Advance another 2s -> Attempt 3 retry (max_retries = 2)
    now_4s = datetime(2026, 10, 30, 10, 0, 6, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_4s)
    retries = [a.command for a in actions if isinstance(a, SendSignalCommandAction)]
    assert len(retries) == 1
    assert retries[0].attempt == 3

    # 3. Advance another 2s -> Exhausted retries -> FAILURE mode!
    now_6s = datetime(2026, 10, 30, 10, 0, 9, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_6s)
    assert state.failure.active
    assert compute_mode(state) == Mode.FAILURE
    # Spec: Direction actual state becomes UNKNOWN, OFFLINE comes only from status events
    assert state.directions["NORTH"].actual == SignalColor.UNKNOWN


def test_aging_and_starvation_scheduler(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "SOUTH": DirectionState(desired=SignalColor.GREEN, actual=SignalColor.GREEN),
        "EAST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
        "WEST": DirectionState(desired=SignalColor.RED, actual=SignalColor.RED),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    # Add 1 TRUCK waiting on NORTH (current green phase)
    ev_truck = SensorEventInput("ev-tr", "A", "NORTH", "VEHICLE_ARRIVED", "TR-1", 1, now, "TRUCK")
    state, _, _, _ = TrafficEngine.decide(state, ev_truck, config, now)

    # Simulate EMPLOYEE_VEHICLE on EAST that arrived at the server 70 seconds ago
    ev_emp = SensorEventInput("ev-emp", "A", "EAST", "VEHICLE_ARRIVED", "EMP-1", 1, now, "EMPLOYEE_VEHICLE")
    state, _, _, _ = TrafficEngine.decide(state, ev_emp, config, now)
    # Set server arrival time to 70s ago to test aging calculation
    state.vehicles["EMP-1"].arrived_at_server = now - timedelta(seconds=70)

    # Advance past min green (10s)
    now_12s = datetime(2026, 10, 30, 10, 0, 12, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_12s)

    # EAST_WEST should win due to aging score (4.5 > 3.0 * 1.25 = 3.75) and switch to YELLOW on NORTH_SOUTH!
    assert state.step.kind == StepKind.YELLOW
    assert state.step.phase == "NORTH_SOUTH"
    assert state.next_target == "EAST_WEST"


def test_failure_recovery_stability_timer(config: DefaultsConfig):
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = {
        "NORTH": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.RECOVERY_RED, None, StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases={"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]},
        updated_at=now,
    )
    # Manually activate failure
    state.failure.active = True
    state.failure.since = now
    state.failure.reason = "Test failure"

    # Tick 1: Starts stability timer
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now)
    assert state.failure.recovery_stable_since == now
    assert state.failure.active is True

    # Tick 2: 5s later (less than recovery_stable_seconds=10s)
    now_5s = datetime(2026, 10, 30, 10, 0, 5, tzinfo=timezone.utc)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_5s)
    assert state.failure.active is True

    # Tick 3: 11s later -> Stability satisfied! Failure exited!
    now_11s = datetime(2026, 10, 30, 10, 0, 11, tzinfo=timezone.utc)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_11s)
    assert state.failure.active is False
    assert state.step.kind == StepKind.ALL_RED
    assert compute_mode(state) == Mode.AUTOMATIC


def test_p1_empty_junction_boot_reaches_green(initial_state: JunctionState, config: DefaultsConfig):
    """P1: Empty junction after boot transitions to ALL_RED and reaches GREEN; never stays in ALL_RED."""
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state, actions, outcome, _ = TrafficEngine.decide(initial_state, StartupInput(), config, now)

    # Controller ACKs all RECOVERY_YELLOW commands at now
    for a in actions:
        if isinstance(a, SendSignalCommandAction):
            cmd = a.command
            reply = ControllerReplyInput(cmd.command_id, "ACK", cmd.requested_state)
            state, _, _, _ = TrafficEngine.decide(state, reply, config, now)

    # Invariant: Yellow dwell must not advance to RED before yellow_seconds (5s)
    now_4_99s = now + timedelta(seconds=4.99)
    state, early_actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_4_99s)
    early_reds = [a for a in early_actions if isinstance(a, SendSignalCommandAction)]
    assert len(early_reds) == 0, "Yellow must not advance to RED at 4.99s"

    # At exactly 5.0s after confirmation, advances to RECOVERY_RED
    now_5s = now + timedelta(seconds=5.0)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_5s)
    for a in actions:
        if isinstance(a, SendSignalCommandAction):
            cmd = a.command
            reply = ControllerReplyInput(cmd.command_id, "ACK", SignalColor.RED)
            state, _, _, _ = TrafficEngine.decide(state, reply, config, now_5s)

    # Start stability timer at now_5s
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_5s)

    # After recovery stable timer (10s), exits failure to ALL_RED
    now_16s = now_5s + timedelta(seconds=11)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_16s)
    assert state.step.kind == StepKind.ALL_RED
    assert state.step.status == StepStatus.CONFIRMED

    # Advance past all_red_seconds (2s)
    now_19s = now_16s + timedelta(seconds=3)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_19s)

    # Must transition to GREEN!
    assert state.step.kind == StepKind.GREEN
    assert state.step.phase is not None
    green_cmds = [a.command for a in actions if isinstance(a, SendSignalCommandAction)]
    assert len(green_cmds) == 2
    for cmd in green_cmds:
        assert cmd.requested_state == SignalColor.GREEN


def test_p2_emergency_min_green_vs_normal_min_green(config: DefaultsConfig):
    """P2: Emergency on other phase triggers switch after green_min_emergency_seconds (3s), while normal waits 10s."""
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN),
        "SOUTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    # Add truck on EAST (normal traffic)
    ev_truck = SensorEventInput("tr-p2", "A", "EAST", "VEHICLE_ARRIVED", "TRK-P2", 1, now, "TRUCK")
    state, _, _, _ = TrafficEngine.decide(state, ev_truck, config, now)

    # At 4s (between 3s and 10s), normal traffic cannot switch
    now_4s = now + timedelta(seconds=4)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_4s)
    assert state.step.kind == StepKind.GREEN

    # Add emergency on EAST
    ev_em = SensorEventInput("em-p2", "A", "EAST", "VEHICLE_ARRIVED", "AMB-P2", 2, now_4s, "EMERGENCY")
    state, _, _, _ = TrafficEngine.decide(state, ev_em, config, now_4s)

    # Emergency preemption can now trigger immediately at 4s because dwell (4s) >= green_min_emergency_seconds (3s)!
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_4s)
    assert state.step.kind == StepKind.YELLOW
    assert state.step.phase == "NORTH_SOUTH"
    assert state.next_target == "EAST_WEST"


def test_p3_emergency_retained_through_extended_failure(config: DefaultsConfig):
    """P3: An emergency waiting through an extended 3-minute FAILURE is still WAITING at recovery."""
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.OFFLINE),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.OFFLINE),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.OFFLINE),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.OFFLINE),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.RECOVERY_RED, None, StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases=phases,
        updated_at=now,
    )
    # Manually activate FAILURE
    state.failure.active = True
    state.failure.since = now
    state.failure.reason = "Hardware offline"

    # Emergency arrives during failure
    ev_em = SensorEventInput("em-p3", "A", "EAST", "VEHICLE_ARRIVED", "AMB-P3", 1, now, "EMERGENCY")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_em, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert state.vehicles["AMB-P3"].status == VehicleStatus.WAITING

    # Advance 180 seconds (3 minutes, well past 120s stale timeout)
    now_180s = now + timedelta(seconds=180)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_180s)

    # Emergency vehicle MUST still be WAITING (not expired during failure!)
    assert state.vehicles["AMB-P3"].status == VehicleStatus.WAITING

    # Restore controllers to ONLINE
    for d in state.directions.values():
        d.signal_controller_status = DeviceStatus.ONLINE

    # Tick for recovery stability (10s)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_180s)
    now_191s = now_180s + timedelta(seconds=11)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_191s)
    assert state.failure.active is False
    assert state.step.kind == StepKind.ALL_RED

    # Advance past all-red (2s)
    now_194s = now_191s + timedelta(seconds=3)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_194s)

    # Emergency phase EAST_WEST must be served immediately upon recovery!
    assert state.step.kind == StepKind.GREEN
    assert state.step.phase == "EAST_WEST"


def test_p4_degraded_mode_fixed_cycle_and_sensor_offline(config: DefaultsConfig):
    """P4: Sensor offline leads to DEGRADED mode, running fixed-time cycles with degraded_green_seconds."""
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    directions = {
        "NORTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN, signal_controller_status=DeviceStatus.ONLINE, sensor_status=DeviceStatus.ONLINE),
        "SOUTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN, signal_controller_status=DeviceStatus.ONLINE, sensor_status=DeviceStatus.ONLINE),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE, sensor_status=DeviceStatus.ONLINE),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, signal_controller_status=DeviceStatus.ONLINE, sensor_status=DeviceStatus.ONLINE),
    }
    state = JunctionState(
        junction_id="A",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now),
        next_target=None,
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    # 1. Sensor on NORTH goes OFFLINE
    dev_ev = DeviceStatusInput("dev-1", "A", DeviceType.SENSOR, DeviceStatus.OFFLINE, now, direction="NORTH")
    state, _, outcome, _ = TrafficEngine.decide(state, dev_ev, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert compute_mode(state) == Mode.DEGRADED

    # 2. Advance time past degraded_green_seconds (20s)
    now_21s = now + timedelta(seconds=21)
    state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now_21s)

    # Should switch to YELLOW for round-robin phase EAST_WEST
    assert state.step.kind == StepKind.YELLOW
    assert state.step.phase == "NORTH_SOUTH"
    assert state.next_target == "EAST_WEST"

    # 3. Sensor comes back ONLINE
    dev_online = DeviceStatusInput("dev-2", "A", DeviceType.SENSOR, DeviceStatus.ONLINE, now_21s, direction="NORTH")
    state, _, outcome2, _ = TrafficEngine.decide(state, dev_online, config, now_21s)
    assert outcome2 == EventOutcome.ACCEPTED
    assert compute_mode(state) == Mode.AUTOMATIC


def test_spec_8_4_table_all_rows(initial_state: JunctionState, config: DefaultsConfig):
    """Verifies all outcomes in Spec Section 8.4: invalid, unknown junction, stale, duplicates, out-of-order, etc."""
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)

    # 1. Unknown junction
    ev_uj = SensorEventInput("e-1", "UNKNOWN_J", "NORTH", "VEHICLE_ARRIVED", "V-1", 1, now, "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(initial_state, ev_uj, config, now)
    assert outcome == EventOutcome.REJECTED_UNKNOWN_JUNCTION

    # 2. Invalid direction
    ev_id = SensorEventInput("e-2", "A", "UPWARDS", "VEHICLE_ARRIVED", "V-1", 1, now, "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_id, config, now)
    assert outcome == EventOutcome.REJECTED_INVALID

    # 3. Stale timestamp (older than max_event_age_seconds=120)
    ev_stale = SensorEventInput("e-3", "A", "NORTH", "VEHICLE_ARRIVED", "V-1", 1, now - timedelta(seconds=200), "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_stale, config, now)
    assert outcome == EventOutcome.REJECTED_STALE

    # 4. Future skew (> max_future_skew_seconds=5)
    ev_future = SensorEventInput("e-4", "A", "NORTH", "VEHICLE_ARRIVED", "V-1", 1, now + timedelta(seconds=20), "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_future, config, now)
    assert outcome == EventOutcome.REJECTED_INVALID

    # 5. Normal arrival
    ev_ok = SensorEventInput("e-5", "A", "NORTH", "VEHICLE_ARRIVED", "V-1", 10, now, "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_ok, config, now)
    assert outcome == EventOutcome.ACCEPTED

    # 6. Duplicate arrival (same vehicle already WAITING)
    ev_dup_veh = SensorEventInput("e-6", "A", "NORTH", "VEHICLE_ARRIVED", "V-1", 11, now, "TRUCK")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_dup_veh, config, now)
    assert outcome == EventOutcome.DUPLICATE_IGNORED

    # 7. Cleared with direction mismatch
    ev_mismatch = SensorEventInput("e-7", "A", "SOUTH", "VEHICLE_CLEARED", "V-1", 12, now)
    state, _, outcome, _ = TrafficEngine.decide(state, ev_mismatch, config, now)
    assert outcome == EventOutcome.IGNORED

    # 8. Cleared out of order (lower sequence_no)
    ev_ooo = SensorEventInput("e-8", "A", "NORTH", "VEHICLE_CLEARED", "V-1", 5, now)
    state, _, outcome, _ = TrafficEngine.decide(state, ev_ooo, config, now)
    assert outcome == EventOutcome.IGNORED_OUT_OF_ORDER

    # 9. Normal clearance
    ev_clr = SensorEventInput("e-9", "A", "NORTH", "VEHICLE_CLEARED", "V-1", 15, now)
    state, _, outcome, _ = TrafficEngine.decide(state, ev_clr, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert state.vehicles["V-1"].status == VehicleStatus.CLEARED

    # 10. Duplicate clearance
    ev_dup_clr = SensorEventInput("e-10", "A", "NORTH", "VEHICLE_CLEARED", "V-1", 16, now)
    state, _, outcome, _ = TrafficEngine.decide(state, ev_dup_clr, config, now)
    assert outcome == EventOutcome.DUPLICATE_IGNORED

    # 11. Unknown vehicle type accepted as UNKNOWN
    ev_unk = SensorEventInput("e-11", "A", "EAST", "VEHICLE_ARRIVED", "V-UNK", 1, now, "MYSTERY_CART")
    state, _, outcome, _ = TrafficEngine.decide(state, ev_unk, config, now)
    assert outcome == EventOutcome.ACCEPTED
    assert state.vehicles["V-UNK"].vehicle_type == VehicleType.UNKNOWN


def test_randomized_safety_property_loop(initial_state: JunctionState, config: DefaultsConfig):
    """Property test: 500 randomized events must NEVER produce conflicting GREEN signals or negative queues."""
    import random
    state = initial_state
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = ["NORTH", "SOUTH", "EAST", "WEST"]
    vtypes = ["TRUCK", "FORKLIFT", "EMPLOYEE_VEHICLE", "EMERGENCY", "UNKNOWN"]

    for i in range(500):
        now += timedelta(milliseconds=200)
        action_type = random.choice(["ARRIVE", "CLEAR", "TICK", "ACK", "ADMIN", "STATUS"])

        if action_type == "ARRIVE":
            ev = SensorEventInput(
                f"rnd-arr-{i}", "A", random.choice(directions), "VEHICLE_ARRIVED",
                f"V-{random.randint(1, 20)}", i + 1, now, random.choice(vtypes)
            )
            state, _, _, _ = TrafficEngine.decide(state, ev, config, now)

        elif action_type == "CLEAR":
            ev = SensorEventInput(
                f"rnd-clr-{i}", "A", random.choice(directions), "VEHICLE_CLEARED",
                f"V-{random.randint(1, 20)}", i + 1, now
            )
            state, _, _, _ = TrafficEngine.decide(state, ev, config, now)

        elif action_type == "TICK":
            state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now)

        elif action_type == "ACK":
            for d, s in state.directions.items():
                if s.pending_command:
                    reply = ControllerReplyInput(s.pending_command.command_id, "ACK", s.pending_command.requested_state)
                    state, _, _, _ = TrafficEngine.decide(state, reply, config, now)
                    break

        elif action_type == "ADMIN":
            admin_cmd = AdminCommandInput(random.choice(["MANUAL_GREEN_REQUEST", "RETURN_TO_AUTOMATIC"]), random.choice(directions))
            state, _, _, _ = TrafficEngine.decide(state, admin_cmd, config, now)

        elif action_type == "STATUS":
            st_ev = DeviceStatusInput(f"st-{i}", "A", random.choice([DeviceType.SIGNAL_CONTROLLER, DeviceType.SENSOR]), random.choice([DeviceStatus.ONLINE, DeviceStatus.OFFLINE]), now, random.choice(directions))
            state, _, _, _ = TrafficEngine.decide(state, st_ev, config, now)

        # Invariant 1: Conflicting phases NEVER both GREEN
        green_phases = set()
        for p_name, p_dirs in state.phases.items():
            for d in p_dirs:
                if state.directions[d].actual == SignalColor.GREEN or state.directions[d].desired == SignalColor.GREEN:
                    green_phases.add(p_name)
        assert len(green_phases) <= 1, f"Safety Violation: Conflicting green phases {green_phases} at step {i}"

        # Invariant 2: Queues are never negative
        for d in directions:
            q = sum(1 for v in state.vehicles.values() if v.direction == d and v.status == VehicleStatus.WAITING)
            assert q >= 0

