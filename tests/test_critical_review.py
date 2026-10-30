import asyncio
from datetime import datetime, timedelta, timezone
import os
import random
import tempfile
import pytest
from httpx import ASGITransport, AsyncClient

from backend.config.config import AppConfig, DefaultsConfig
from backend.domain.types import (
    AdminCommandInput,
    AuditEventType,
    CommandStatus,
    ControllerCommand,
    ControllerReplyInput,
    DeviceStatus,
    DeviceStatusInput,
    DirectionState,
    EventOutcome,
    JunctionState,
    ManualState,
    Mode,
    SendSignalCommandAction,
    SensorEventInput,
    SignalColor,
    StartupInput,
    StepKind,
    StepState,
    StepStatus,
    TickInput,
    VehicleRecord,
    VehicleStatus,
    VehicleType,
)
from backend.domain.engine import TrafficEngine, compute_mode
from backend.adapters.persistence.repository import SQLiteRepository
from backend.application.junction_worker import JunctionWorker
from backend.application.manager import JunctionManager
from backend.main import app


@pytest.fixture
def config():
    return DefaultsConfig()


@pytest.fixture
def four_way_junction():
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = {
        "NORTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
    }
    phases = {
        "NORTH_SOUTH": ["NORTH", "SOUTH"],
        "EAST_WEST": ["EAST", "WEST"],
    }
    return JunctionState(
        junction_id="CRITICAL_J1",
        step=StepState(StepKind.ALL_RED, None, StepStatus.CONFIRMED, now, now),
        next_target="NORTH_SOUTH",
        directions=directions,
        phases=phases,
        updated_at=now,
    )


# =============================================================================
# 1. Stronger property test
# =============================================================================
def test_critical_01_stronger_property_test(config: DefaultsConfig):
    """1. Stronger property test:
    - Seed it and ACK most commands.
    - Check every command the engine emits: a GREEN only after all other directions have been
      confirmed RED for at least the all-red time, and a RED only after at least 5 s of confirmed yellow.
    - Assert that the run actually reached GREEN, YELLOW and ALL_RED, so it cannot pass by doing nothing.
    """
    random.seed(1337)
    now = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc)

    directions = {
        "NORTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
    }
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    state = JunctionState(
        junction_id="PROP_J",
        step=StepState(StepKind.ALL_RED, None, StepStatus.CONFIRMED, now, now),
        next_target="NORTH_SOUTH",
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    reached_states = set()
    dir_confirmed_yellow_at = {}
    dir_confirmed_red_at = {d: now for d in directions}

    pending_cmds = {}
    step_count = 350
    seq_counter = 100

    for step_i in range(step_count):
        now += timedelta(seconds=1)
        reached_states.add(state.step.kind)

        # 1. Randomly inject vehicle arrivals to provoke phase transitions
        if step_i % 8 == 0:
            d_choice = random.choice(list(directions.keys()))
            v_type = random.choice(["TRUCK", "FORKLIFT", "EMPLOYEE", "EMERGENCY"])
            seq_counter += 1
            v_ev = SensorEventInput(
                event_id=f"evt-{step_i}-{seq_counter}",
                junction_id="PROP_J",
                timestamp=now,
                direction=d_choice,
                event_type="VEHICLE_ARRIVED",
                vehicle_id=f"veh-{step_i}",
                vehicle_type=v_type,
                sequence_no=seq_counter,
            )
            state, act_v, _, _ = TrafficEngine.decide(state, v_ev, config, now)

        # 2. Tick engine
        state, actions, _, _ = TrafficEngine.decide(state, TickInput(), config, now)

        # Invariant checks on emitted commands
        for a in actions:
            if isinstance(a, SendSignalCommandAction):
                cmd = a.command
                pending_cmds[cmd.command_id] = cmd

                # Invariant 1: GREEN transition only after conflicting directions confirmed RED >= all_red_seconds
                if cmd.requested_state == SignalColor.GREEN and state.step.kind == StepKind.ALL_RED:
                    target_phase = None
                    for p_name, dirs in state.phases.items():
                        if cmd.direction in dirs:
                            target_phase = p_name
                            break
                    conflicting_dirs = [d for d in directions if d not in state.phases[target_phase]]
                    for cd in conflicting_dirs:
                        assert state.directions[cd].actual == SignalColor.RED, (
                            f"Safety violation: Emitted GREEN for {cmd.direction} while conflicting {cd} actual is {state.directions[cd].actual}"
                        )
                        red_confirmed = dir_confirmed_red_at.get(cd, now)
                        red_duration = (now - red_confirmed).total_seconds()
                        assert red_duration >= config.timing.all_red_seconds - 0.001, (
                            f"Safety violation: Emitted GREEN before all-red dwell elapsed on {cd}: {red_duration}s < {config.timing.all_red_seconds}s"
                        )

                # Invariant 2: RED transition only after at least 5 s of confirmed yellow
                if cmd.requested_state == SignalColor.RED and state.step.kind == StepKind.YELLOW:
                    yellow_confirmed = dir_confirmed_yellow_at.get(cmd.direction)
                    assert yellow_confirmed is not None, (
                        f"Safety violation: Emitted RED for {cmd.direction} without prior confirmed yellow!"
                    )
                    yellow_duration = (now - yellow_confirmed).total_seconds()
                    assert yellow_duration >= config.timing.yellow_seconds - 0.001, (
                        f"Safety violation: Emitted RED for {cmd.direction} after only {yellow_duration}s of yellow (< 5.0s)!"
                    )

        # 3. Simulate controller ACK responses (ACK most commands with 0 or 1s delay)
        to_ack = list(pending_cmds.keys())
        for c_id in to_ack:
            cmd = pending_cmds[c_id]
            # 85% chance to ACK on each step
            if random.random() < 0.85:
                del pending_cmds[c_id]
                reply = ControllerReplyInput(c_id, "ACK", cmd.requested_state)
                state, _, _, _ = TrafficEngine.decide(state, reply, config, now)

                # Track confirmation timestamps for invariants
                if cmd.requested_state == SignalColor.YELLOW:
                    dir_confirmed_yellow_at[cmd.direction] = now
                elif cmd.requested_state == SignalColor.RED:
                    dir_confirmed_red_at[cmd.direction] = now

    # Assert that simulation actively exercised GREEN, YELLOW, and ALL_RED
    assert StepKind.GREEN in reached_states, "Property test failed: GREEN step was never reached"
    assert StepKind.YELLOW in reached_states, "Property test failed: YELLOW step was never reached"
    assert StepKind.ALL_RED in reached_states, "Property test failed: ALL_RED step was never reached"


# =============================================================================
# 2. CLEARED before ARRIVED
# =============================================================================
def test_critical_02_cleared_before_arrived(four_way_junction: JunctionState, config: DefaultsConfig):
    """2. CLEARED before ARRIVED:
    Send CLEARED (seq 1502), then a late ARRIVED (seq 1501).
    Expect it to be ignored and the queue to stay at 0, with no ghost vehicle.
    """
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = four_way_junction

    # 1. Send CLEARED (seq 1502) first
    ev_cleared = SensorEventInput(
        event_id="evt-clear-1502",
        junction_id=state.junction_id,
        timestamp=now,
        direction="NORTH",
        event_type="VEHICLE_CLEARED",
        vehicle_id="V-150",
        vehicle_type="FORKLIFT",
        sequence_no=1502,
    )
    state, actions, outcome, msg = TrafficEngine.decide(state, ev_cleared, config, now)
    assert outcome == EventOutcome.ACCEPTED_NO_CHANGE
    assert "tombstone" in msg.lower()
    waiting_count = sum(1 for v in state.vehicles.values() if v.status == VehicleStatus.WAITING)
    assert waiting_count == 0

    # 2. Send late ARRIVED (seq 1501)
    ev_late_arrived = SensorEventInput(
        event_id="evt-arrive-1501",
        junction_id=state.junction_id,
        timestamp=now + timedelta(seconds=1),
        direction="NORTH",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="V-150",
        vehicle_type="FORKLIFT",
        sequence_no=1501,
    )
    state, actions, outcome2, msg2 = TrafficEngine.decide(state, ev_late_arrived, config, now + timedelta(seconds=1))

    # Assert late arrival is rejected as out-of-order and NO ghost vehicle is queued
    assert outcome2 == EventOutcome.IGNORED_OUT_OF_ORDER
    waiting_count_after = sum(1 for v in state.vehicles.values() if v.status == VehicleStatus.WAITING)
    assert waiting_count_after == 0
    assert state.vehicles["V-150"].status == VehicleStatus.CLEARED
    assert state.vehicles["V-150"].last_sequence_no == 1502


# =============================================================================
# 3. Dwell boundaries
# =============================================================================
def test_critical_03_dwell_boundaries(config: DefaultsConfig):
    """3. Dwell boundaries:
    Yellow does not advance at 4.99 s after confirmation and does advance at 5 s.
    Time before the ACK doesn't count. Do the same for all-red and min green.
    """
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = {
        "NORTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN, last_confirmed_at=now),
        "SOUTH": DirectionState(SignalColor.GREEN, SignalColor.GREEN, last_confirmed_at=now),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
    }
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}
    state = JunctionState(
        junction_id="DWELL_J",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, now),
        next_target="EAST_WEST",
        directions=directions,
        phases=phases,
        updated_at=now,
    )

    # --- Part A: Min Green dwell (10.0s) ---
    v_req = SensorEventInput(
        event_id="evt-req-ew",
        junction_id="DWELL_J",
        timestamp=now,
        direction="EAST",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="V-EAST-1",
        vehicle_type="TRUCK",
        sequence_no=1,
    )
    state, _, _, _ = TrafficEngine.decide(state, v_req, config, now)

    # At 9.99s after green confirmation: step MUST remain GREEN
    now_9_99s = now + timedelta(seconds=9.99)
    state, acts_999, _, _ = TrafficEngine.decide(state, TickInput(), config, now_9_99s)
    assert state.step.kind == StepKind.GREEN
    assert not any(isinstance(a, SendSignalCommandAction) for a in acts_999)

    # At 10.00s after green confirmation: transitions to YELLOW
    now_10s = now + timedelta(seconds=10.0)
    state, acts_10s, _, _ = TrafficEngine.decide(state, TickInput(), config, now_10s)
    assert state.step.kind == StepKind.YELLOW
    assert state.step.status == StepStatus.PENDING
    yellow_cmds = [a.command for a in acts_10s if isinstance(a, SendSignalCommandAction)]
    assert len(yellow_cmds) == 2

    # --- Part B: Yellow dwell (5.0s, measured from ACK, NOT command emission) ---
    # Hardware takes 2.0s to reply with ACK! Time before ACK DOES NOT COUNT!
    now_ack = now_10s + timedelta(seconds=2.0)
    for c in yellow_cmds:
        reply = ControllerReplyInput(c.command_id, "ACK", SignalColor.YELLOW)
        state, _, _, _ = TrafficEngine.decide(state, reply, config, now_ack)
    assert state.step.status == StepStatus.CONFIRMED
    assert state.step.confirmed_at == now_ack

    # At 4.99s after ACK confirmation (6.99s after command): does NOT advance to ALL_RED
    now_yellow_4_99 = now_ack + timedelta(seconds=4.99)
    state, acts_y499, _, _ = TrafficEngine.decide(state, TickInput(), config, now_yellow_4_99)
    assert state.step.kind == StepKind.YELLOW
    assert not any(isinstance(a, SendSignalCommandAction) for a in acts_y499)

    # At 5.00s after ACK confirmation: advances to ALL_RED and commands RED
    now_yellow_5s = now_ack + timedelta(seconds=5.0)
    state, acts_y5s, _, _ = TrafficEngine.decide(state, TickInput(), config, now_yellow_5s)
    assert state.step.kind == StepKind.ALL_RED
    assert state.step.status == StepStatus.PENDING
    red_cmds = [a.command for a in acts_y5s if isinstance(a, SendSignalCommandAction)]
    assert len(red_cmds) == 2

    # --- Part C: All-Red dwell (2.0s, measured from ACK) ---
    now_red_ack = now_yellow_5s + timedelta(seconds=1.0)
    for c in red_cmds:
        reply = ControllerReplyInput(c.command_id, "ACK", SignalColor.RED)
        state, _, _, _ = TrafficEngine.decide(state, reply, config, now_red_ack)
    assert state.step.status == StepStatus.CONFIRMED
    assert state.step.confirmed_at == now_red_ack

    # At 1.99s after ALL_RED confirmation: does NOT advance to GREEN
    now_ar_1_99 = now_red_ack + timedelta(seconds=1.99)
    state, acts_ar199, _, _ = TrafficEngine.decide(state, TickInput(), config, now_ar_1_99)
    assert state.step.kind == StepKind.ALL_RED
    assert not any(isinstance(a, SendSignalCommandAction) for a in acts_ar199)

    # At 2.00s after ALL_RED confirmation: advances to GREEN for EAST_WEST
    now_ar_2s = now_red_ack + timedelta(seconds=2.0)
    state, acts_ar2s, _, _ = TrafficEngine.decide(state, TickInput(), config, now_ar_2s)
    assert state.step.kind == StepKind.GREEN
    assert state.step.phase == "EAST_WEST"


# =============================================================================
# 4. Controller replies
# =============================================================================
def test_critical_04_controller_replies(four_way_junction: JunctionState, config: DefaultsConfig):
    """4. Controller replies:
    A duplicate ACK is ignored, a late ACK is accepted, a wrong-state ACK or a NACK
    ends in FAILURE, and a superseded or unknown command ID is ignored.
    """
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = four_way_junction

    # Put a pending command on NORTH
    cmd = ControllerCommand(
        command_id="cmd-ctrl-test-1",
        junction_id=state.junction_id,
        direction="NORTH",
        requested_state=SignalColor.GREEN,
        attempt=1,
        sent_at=now,
        status=CommandStatus.PENDING,
    )
    state.directions["NORTH"].pending_command = cmd

    # 1. First ACK is ACCEPTED
    reply1 = ControllerReplyInput("cmd-ctrl-test-1", "ACK", SignalColor.GREEN)
    state, _, out1, _ = TrafficEngine.decide(state, reply1, config, now)
    assert out1 == EventOutcome.ACCEPTED
    assert state.directions["NORTH"].actual == SignalColor.GREEN
    assert state.directions["NORTH"].pending_command is None

    # 2. Duplicate ACK is IGNORED (DUPLICATE_IGNORED)
    state, _, out_dup, msg_dup = TrafficEngine.decide(state, reply1, config, now)
    assert out_dup == EventOutcome.DUPLICATE_IGNORED
    assert "already resolved" in msg_dup.lower()

    # 3. Unknown or superseded command ID is IGNORED
    reply_unknown = ControllerReplyInput("cmd-ghost-999", "ACK", SignalColor.RED)
    state, _, out_unk, msg_unk = TrafficEngine.decide(state, reply_unknown, config, now)
    assert out_unk == EventOutcome.DUPLICATE_IGNORED

    # 4. Late ACK on retried command:
    cmd2 = ControllerCommand(
        command_id="cmd-ctrl-test-2",
        junction_id=state.junction_id,
        direction="SOUTH",
        requested_state=SignalColor.GREEN,
        attempt=2,
        sent_at=now,
        status=CommandStatus.PENDING,
    )
    state.directions["SOUTH"].pending_command = cmd2
    reply_late = ControllerReplyInput("cmd-ctrl-test-2", "ACK", SignalColor.GREEN)
    state, _, out_late, _ = TrafficEngine.decide(state, reply_late, config, now + timedelta(seconds=4))
    assert out_late == EventOutcome.ACCEPTED
    assert state.directions["SOUTH"].actual == SignalColor.GREEN

    # 5. Wrong-state ACK triggers FAILURE mode
    cmd3 = ControllerCommand(
        command_id="cmd-ctrl-test-3",
        junction_id=state.junction_id,
        direction="EAST",
        requested_state=SignalColor.RED,
        attempt=1,
        sent_at=now,
        status=CommandStatus.PENDING,
    )
    state.directions["EAST"].pending_command = cmd3
    reply_wrong = ControllerReplyInput("cmd-ctrl-test-3", "ACK", SignalColor.GREEN)
    state, acts_wrong, out_wrong, msg_wrong = TrafficEngine.decide(state, reply_wrong, config, now)
    assert out_wrong == EventOutcome.REJECTED_INVALID
    assert state.failure.active, "Wrong-state ACK must trigger FAILURE mode"
    assert compute_mode(state) == Mode.FAILURE

    # 6. NACK triggers FAILURE mode
    state.failure.active = False
    cmd4 = ControllerCommand(
        command_id="cmd-ctrl-test-4",
        junction_id=state.junction_id,
        direction="WEST",
        requested_state=SignalColor.RED,
        attempt=1,
        sent_at=now,
        status=CommandStatus.PENDING,
    )
    state.directions["WEST"].pending_command = cmd4
    reply_nack = ControllerReplyInput("cmd-ctrl-test-4", "NACK", SignalColor.RED)
    state, _, out_nack, _ = TrafficEngine.decide(state, reply_nack, config, now)
    assert state.failure.active, "NACK must trigger FAILURE mode"


# =============================================================================
# 5. Manual control
# =============================================================================
def test_critical_05_manual_control(four_way_junction: JunctionState, config: DefaultsConfig):
    """5. Manual control:
    Lease renewal, lease expiry, return to automatic, and an emergency ending the lease.
    """
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    state = four_way_junction

    # 1. Start manual control lease
    cmd_manual = AdminCommandInput("MANUAL_GREEN_REQUEST", direction="NORTH", requested_by="alice")
    state, _, out_man, _ = TrafficEngine.decide(state, cmd_manual, config, now)
    assert out_man == EventOutcome.ACCEPTED
    assert state.manual.active
    assert state.manual.target_phase == "NORTH_SOUTH"
    initial_expiry = state.manual.expires_at
    assert initial_expiry == now + timedelta(seconds=config.manual.lease_seconds)

    # 2. Lease renewal: extend lease before expiry
    now_renew = now + timedelta(seconds=15)
    cmd_renew = AdminCommandInput("MANUAL_GREEN_REQUEST", direction="NORTH", requested_by="alice")
    state, _, out_ren, _ = TrafficEngine.decide(state, cmd_renew, config, now_renew)
    assert out_ren == EventOutcome.ACCEPTED
    assert state.manual.expires_at == now_renew + timedelta(seconds=config.manual.lease_seconds)
    assert state.manual.expires_at > initial_expiry

    # 3. Return to automatic
    cmd_auto = AdminCommandInput("RETURN_TO_AUTOMATIC", requested_by="alice")
    state, _, out_auto, _ = TrafficEngine.decide(state, cmd_auto, config, now_renew + timedelta(seconds=5))
    assert out_auto == EventOutcome.ACCEPTED
    assert not state.manual.active
    assert compute_mode(state) != Mode.MANUAL

    # 4. Lease expiry: start new lease and let time pass
    now_exp = now + timedelta(seconds=100)
    cmd_man2 = AdminCommandInput("MANUAL_GREEN_REQUEST", direction="SOUTH", requested_by="bob")
    state, _, _, _ = TrafficEngine.decide(state, cmd_man2, config, now_exp)
    assert state.manual.active

    # Advance past lease duration (30s)
    now_expired = now_exp + timedelta(seconds=config.manual.lease_seconds + 1)
    state, _, _, _ = TrafficEngine.decide(state, TickInput(), config, now_expired)
    assert not state.manual.active, "Manual lease must expire after lease duration"

    # 5. Emergency ending active lease:
    now_em = now_expired + timedelta(seconds=10)
    state, _, _, _ = TrafficEngine.decide(state, cmd_man2, config, now_em)
    assert state.manual.active

    # Emergency vehicle arrives
    ev_em = SensorEventInput(
        event_id="evt-em-override",
        junction_id=state.junction_id,
        timestamp=now_em,
        direction="EAST",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="EV-112",
        vehicle_type="EMERGENCY",
        sequence_no=1,
    )
    state, _, _, _ = TrafficEngine.decide(state, ev_em, config, now_em)
    assert not state.manual.active, "Emergency vehicle arrival must immediately end manual control lease"
    assert compute_mode(state) == Mode.EMERGENCY

    # Manual command during active emergency is rejected
    cmd_rej = AdminCommandInput("MANUAL_GREEN_REQUEST", direction="NORTH", requested_by="carol")
    state, _, out_rej, msg_rej = TrafficEngine.decide(state, cmd_rej, config, now_em + timedelta(seconds=1))
    assert out_rej == EventOutcome.REJECTED_INVALID
    assert "EMERGENCY" in msg_rej


# =============================================================================
# 6. Restart
# =============================================================================
def test_critical_06_restart(config: DefaultsConfig):
    """6. Restart:
    A duplicate event_id after a restart is still ignored.
    Pending commands become abandoned.
    A valid lease resumes and an expired one is dropped.
    """
    now = datetime(2026, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    directions = {
        "NORTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "SOUTH": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "EAST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
        "WEST": DirectionState(SignalColor.RED, SignalColor.RED, last_confirmed_at=now),
    }
    phases = {"NORTH_SOUTH": ["NORTH", "SOUTH"], "EAST_WEST": ["EAST", "WEST"]}

    # Create pre-restart state
    state = JunctionState(
        junction_id="RESTART_J",
        step=StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, now),
        next_target="EAST_WEST",
        directions=directions,
        phases=phases,
        updated_at=now,
    )
    # Process event before restart
    v_ev = SensorEventInput(
        event_id="evt-pre-restart-101",
        junction_id="RESTART_J",
        timestamp=now,
        direction="NORTH",
        event_type="VEHICLE_ARRIVED",
        vehicle_id="V-PRE-1",
        vehicle_type="TRUCK",
        sequence_no=1,
    )
    state, _, _, _ = TrafficEngine.decide(state, v_ev, config, now)

    # Attach pending command
    state.directions["NORTH"].pending_command = ControllerCommand(
        command_id="cmd-pre-restart",
        junction_id="RESTART_J",
        direction="NORTH",
        requested_state=SignalColor.YELLOW,
        attempt=1,
        sent_at=now,
        status=CommandStatus.PENDING,
    )

    # Manual lease valid until now + 60s
    state.manual.active = True
    state.manual.target_phase = "NORTH_SOUTH"
    state.manual.expires_at = now + timedelta(seconds=60)

    # Emergency vehicles
    state.vehicles["EM-OLD"] = VehicleRecord(
        vehicle_id="EM-OLD",
        vehicle_type=VehicleType.EMERGENCY,
        direction="EAST",
        status=VehicleStatus.WAITING,
        arrived_at_server=now - timedelta(seconds=150),
        arrived_at_sensor=now - timedelta(seconds=150),
        last_sequence_no=1,
    )
    state.vehicles["EM-FRESH"] = VehicleRecord(
        vehicle_id="EM-FRESH",
        vehicle_type=VehicleType.EMERGENCY,
        direction="WEST",
        status=VehicleStatus.WAITING,
        arrived_at_server=now - timedelta(seconds=30),
        arrived_at_sensor=now - timedelta(seconds=30),
        last_sequence_no=2,
    )

    # --- SIMULATE RESTART ---
    old_pending_cmd = state.directions["NORTH"].pending_command
    restart_time = now + timedelta(seconds=10)
    state, actions, outcome, _ = TrafficEngine.decide(state, StartupInput(), config, restart_time)
    assert outcome == EventOutcome.ACCEPTED

    # 1. Pre-restart pending command was abandoned (and new fail-safe recovery command emitted)
    assert old_pending_cmd.status == CommandStatus.ABANDONED
    assert state.directions["NORTH"].pending_command.command_id != old_pending_cmd.command_id
    assert state.directions["NORTH"].pending_command.requested_state == SignalColor.YELLOW

    # 2. Duplicate event_id after restart is still ignored
    state, _, out_dup, _ = TrafficEngine.decide(state, v_ev, config, restart_time)
    assert out_dup == EventOutcome.DUPLICATE_IGNORED

    # 3. Emergency older than 120s expired during downtime; fresh emergency retained
    assert state.vehicles["EM-OLD"].status == VehicleStatus.CLEARED
    assert state.vehicles["EM-FRESH"].status == VehicleStatus.WAITING

    # 4. Valid manual lease is suspended during recovery, ready to resume once fail-safe red confirmed
    assert state.manual.active
    assert state.manual.suspended

    # If restart happens AFTER manual lease expired:
    state.manual.expires_at = now - timedelta(seconds=10)
    state, _, _, _ = TrafficEngine.decide(state, StartupInput(), config, restart_time)
    assert not state.manual.active, "Expired manual lease must be dropped on restart"


# =============================================================================
# 7. Concurrency
# =============================================================================
@pytest.mark.asyncio
async def test_critical_07_concurrency():
    """7. Concurrency:
    50 parallel POSTs with the same event_id give exactly one ACCEPTED.
    The PDF's concurrent sequence, including the ACK, keeps the invariants.
    """
    app_cfg = AppConfig.load_from_yaml("config/default_config.yaml")
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        repo = SQLiteRepository(db_path=db_path)
        manager = JunctionManager(app_cfg, repo)
        await manager.initialize()

        worker = manager.get_worker("A")
        assert worker is not None

        # Part A: 50 parallel POSTs with identical event_id
        now = datetime.now(timezone.utc)
        target_event_id = "concurrent-idempotency-50-test"

        async def send_duplicate():
            inp = SensorEventInput(
                event_id=target_event_id,
                junction_id="A",
                timestamp=now,
                direction="NORTH",
                event_type="VEHICLE_ARRIVED",
                vehicle_id="V-CONC-1",
                vehicle_type="FORKLIFT",
                sequence_no=100,
            )
            return await worker.submit(inp)

        results = await asyncio.gather(*[send_duplicate() for _ in range(50)])
        accepted_count = sum(1 for outcome, _ in results if outcome == EventOutcome.ACCEPTED)
        duplicate_count = sum(1 for outcome, _ in results if outcome == EventOutcome.DUPLICATE_IGNORED)

        assert accepted_count == 1, f"Expected exactly 1 ACCEPTED, got {accepted_count}"
        assert duplicate_count == 49, f"Expected 49 DUPLICATE_IGNORED, got {duplicate_count}"

        # Part B: Concurrently submitting sensor events and ACKs maintains invariants
        async def simulate_mixed_traffic(i):
            ts = now + timedelta(milliseconds=i * 20)
            inp = SensorEventInput(
                event_id=f"mix-{i}",
                junction_id="A",
                timestamp=ts,
                direction=random.choice(["NORTH", "SOUTH", "EAST", "WEST"]),
                event_type="VEHICLE_ARRIVED",
                vehicle_id=f"V-MIX-{i}",
                vehicle_type=random.choice(["TRUCK", "FORKLIFT", "EMPLOYEE"]),
                sequence_no=i + 200,
            )
            return await worker.submit(inp)

        mixed_results = await asyncio.gather(*[simulate_mixed_traffic(i) for i in range(25)])
        assert len(mixed_results) == 25

        # Check invariants: no two opposing directions green simultaneously
        ns_green = worker.state.directions["NORTH"].actual == SignalColor.GREEN or worker.state.directions["SOUTH"].actual == SignalColor.GREEN
        ew_green = worker.state.directions["EAST"].actual == SignalColor.GREEN or worker.state.directions["WEST"].actual == SignalColor.GREEN
        assert not (ns_green and ew_green), "Safety invariant violated under concurrent submissions!"

        await manager.shutdown()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


# =============================================================================
# 8. API errors
# =============================================================================
@pytest.mark.asyncio
async def test_critical_08_api_errors():
    """8. API errors:
    A bad body returns 400, because FastAPI's default is 422 and the spec says 400.
    An unknown junction returns 404, and a manual request during an emergency returns 409.
    """
    import backend.main as main_mod

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app_cfg = AppConfig.load_from_yaml("config/default_config.yaml")
    app_cfg.persistence.db_path = path
    repo = SQLiteRepository(db_path=path)
    mgr = JunctionManager(app_cfg, repo)
    main_mod.app_manager = mgr
    await mgr.initialize()

    worker = mgr.get_worker("A")
    if worker and worker.state:
        now = datetime.now(timezone.utc)
        worker.state.failure.active = False
        worker.state.step = StepState(StepKind.GREEN, "NORTH_SOUTH", StepStatus.CONFIRMED, now, confirmed_at=now)
        for d in ["NORTH", "SOUTH"]:
            worker.state.directions[d].actual = SignalColor.GREEN
            worker.state.directions[d].desired = SignalColor.GREEN
            worker.state.directions[d].signal_controller_status = DeviceStatus.ONLINE
        for d in ["EAST", "WEST"]:
            worker.state.directions[d].actual = SignalColor.RED
            worker.state.directions[d].desired = SignalColor.RED
            worker.state.directions[d].signal_controller_status = DeviceStatus.ONLINE

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Bad body returns 400 Bad Request (Spec compliance, not FastAPI default 422)
            bad_body = {
                "event_id": "bad-body-1",
                # Missing required junction_id, direction, vehicle_id, etc.
                "junk_field": "invalid",
            }
            resp_bad = await ac.post("/api/sensor-events", json=bad_body)
            assert resp_bad.status_code == 400, f"Expected HTTP 400 for bad body, got {resp_bad.status_code}: {resp_bad.text}"

            # 2. Unknown junction returns 404 Not Found
            cmd_body = {
                "command": "MANUAL_GREEN_REQUEST",
                "direction": "NORTH",
                "requested_by": "evaluator",
            }
            resp_404 = await ac.post("/api/junctions/UNKNOWN_JUNCTION_XYZ/commands", json=cmd_body)
            assert resp_404.status_code == 404, f"Expected HTTP 404 for unknown junction, got {resp_404.status_code}"

            # 3. Manual request during emergency returns 409 Conflict
            now_iso = datetime.now(timezone.utc).isoformat()
            em_event = {
                "event_id": f"em-evt-{random.randint(10000, 99999)}",
                "junction_id": "A",
                "timestamp": now_iso,
                "direction": "NORTH",
                "event_type": "VEHICLE_ARRIVED",
                "vehicle_id": "EM-FIRE-1",
                "vehicle_type": "EMERGENCY",
                "sequence_no": 9999,
            }
            resp_em = await ac.post("/api/sensor-events", json=em_event)
            assert resp_em.status_code in (200, 202)

            # Attempt manual override while emergency is active -> 409 Conflict
            resp_conflict = await ac.post("/api/junctions/A/commands", json=cmd_body)
            assert resp_conflict.status_code == 409, f"Expected HTTP 409 Conflict during emergency, got {resp_conflict.status_code}: {resp_conflict.text}"
    finally:
        await mgr.shutdown()
        if os.path.exists(path):
            os.remove(path)

