from datetime import datetime, timedelta
import hashlib
from typing import Dict, List, Optional, Tuple
from backend.config.config import DefaultsConfig
from backend.domain.emergency import EmergencyHandler
from backend.domain.guard import SafetyGuard
from backend.domain.scheduler import TrafficScheduler
from backend.domain.types import (
    AdminCommandInput,
    AuditEventType,
    AuditRecord,
    AuditSeverity,
    CommandStatus,
    ControllerCommand,
    ControllerReplyInput,
    DeviceStatus,
    DeviceStatusInput,
    DeviceType,
    DirectionState,
    EngineAction,
    EngineInput,
    EventOutcome,
    JunctionState,
    ManualState,
    Mode,
    ProcessedEventRecord,
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


def compute_mode(state: JunctionState) -> Mode:
    """Derives current junction mode from stored facts with precedence:

    FAILURE > EMERGENCY > MANUAL > DEGRADED > AUTOMATIC.
    """
    if state.failure.active:
        return Mode.FAILURE

    # Any waiting emergency vehicle?
    has_emergency = any(
        v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY
        for v in state.vehicles.values()
    )
    if has_emergency:
        return Mode.EMERGENCY

    # Active, unsuspended manual lease?
    if state.manual.active and not state.manual.suspended:
        return Mode.MANUAL

    # Any sensor status other than ONLINE / WARNING?
    has_unreliable_sensor = any(
        d.sensor_status in (DeviceStatus.OFFLINE, DeviceStatus.DEGRADED, DeviceStatus.UNKNOWN)
        for d in state.directions.values()
    )
    if has_unreliable_sensor:
        return Mode.DEGRADED

    return Mode.AUTOMATIC


def create_initial_junction_state(
    junction_id: str,
    directions: List[str],
    phases: Dict[str, List[str]],
    now: datetime,
) -> JunctionState:
    """Creates a fresh junction state with all actual states UNKNOWN, starting in recovery."""
    dir_states: Dict[str, DirectionState] = {}
    for d in directions:
        dir_states[d] = DirectionState(
            desired=SignalColor.RED,
            actual=SignalColor.UNKNOWN,
            signal_controller_status=DeviceStatus.UNKNOWN,
            sensor_status=DeviceStatus.ONLINE,
        )

    # Initial step is RECOVERY_RED, pending until confirmed
    step = StepState(
        kind=StepKind.RECOVERY_RED,
        phase=None,
        status=StepStatus.PENDING,
        started_at=now,
        confirmed_at=None,
    )

    return JunctionState(
        junction_id=junction_id,
        step=step,
        next_target=None,
        directions=dir_states,
        phases=phases,
        updated_at=now,
    )


class TrafficEngine:
    """Pure domain logic engine for a single traffic junction.

    No I/O, no DB access, no system clock calls. Deterministic decide() contract.
    """

    @classmethod
    def decide(
        cls,
        state: JunctionState,
        event_input: EngineInput,
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[JunctionState, List[EngineAction], Optional[EventOutcome], str]:
        """Core pure function: decide(state, input, config, now) -> (newState, actions, outcome, message)."""
        actions: List[EngineAction] = []
        old_mode = compute_mode(state)

        outcome: Optional[EventOutcome] = None
        message: str = "OK"

        if isinstance(event_input, StartupInput):
            outcome, message = cls._handle_startup(state, actions, config, now)

        elif isinstance(event_input, SensorEventInput):
            outcome, message = cls._handle_sensor_event(state, event_input, actions, config, now)

        elif isinstance(event_input, AdminCommandInput):
            outcome, message = cls._handle_admin_command(state, event_input, actions, config, now)

        elif isinstance(event_input, ControllerReplyInput):
            outcome, message = cls._handle_controller_reply(state, event_input, actions, config, now)

        elif isinstance(event_input, DeviceStatusInput):
            outcome, message = cls._handle_device_status(state, event_input, actions, config, now)

        elif isinstance(event_input, TickInput):
            outcome, message = cls._handle_tick(state, actions, config, now)

        # Check if mode changed as a result of state changes
        new_mode = compute_mode(state)
        if new_mode != old_mode:
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.MODE_CHANGED.value,
                    severity=AuditSeverity.INFO,
                    mode=new_mode,
                    reason=f"Mode changed from {old_mode.value} to {new_mode.value}",
                )
            )

        state.updated_at = now
        return state, actions, outcome, message

    # -------------------------------------------------------------------------
    # Helper: Command Emission with Safety Guard (Layer 2)
    # -------------------------------------------------------------------------
    @classmethod
    def _emit_command(
        cls,
        state: JunctionState,
        direction: str,
        requested_color: SignalColor,
        target_phase: Optional[str],
        actions: List[EngineAction],
        now: datetime,
        transition_id: Optional[str] = None,
    ) -> bool:
        """Constructs and validates a controller command through the Safety Guard."""
        state.command_counter += 1
        cmd_id = f"cmd-{state.junction_id}-{state.command_counter:04d}"

        cmd = ControllerCommand(
            command_id=cmd_id,
            junction_id=state.junction_id,
            direction=direction,
            requested_state=requested_color,
            attempt=1,
            sent_at=now,
            status=CommandStatus.PENDING,
        )

        is_safe, reason = SafetyGuard.validate_command(cmd, target_phase, state.phases, state.directions)

        if not is_safe:
            # Bug-level block: Enter FAILURE and fail-safe sequence
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.SAFETY_BLOCKED.value,
                    severity=AuditSeverity.ERROR,
                    direction=direction,
                    command_id=cmd_id,
                    mode=compute_mode(state),
                    reason=f"Safety Guard BLOCKED command {requested_color} for {direction}: {reason}",
                )
            )
            cls._enter_failure(state, f"Safety Guard blocked invalid transition: {reason}", actions, now)
            return False

        # Apply desired state and record pending command
        dir_state = state.directions[direction]
        dir_state.desired = requested_color
        dir_state.pending_command = cmd

        actions.append(
            AuditRecord(
                junction_id=state.junction_id,
                timestamp=now,
                event_type=AuditEventType.SIGNAL_STATE_REQUESTED.value,
                severity=AuditSeverity.INFO,
                direction=direction,
                new_state=requested_color,
                command_id=cmd_id,
                transition_id=transition_id,
                mode=compute_mode(state),
                reason=f"Requested {direction} -> {requested_color.value}",
            )
        )
        actions.append(SendSignalCommandAction(cmd))
        return True

    # -------------------------------------------------------------------------
    # Failure & Recovery Handling (Section 16)
    # -------------------------------------------------------------------------
    @classmethod
    def _enter_failure(
        cls,
        state: JunctionState,
        reason: str,
        actions: List[EngineAction],
        now: datetime,
        config: Optional[DefaultsConfig] = None,
    ) -> None:
        """Enters FAILURE mode and drives lights to safe ALL_RED via YELLOW."""
        if not state.failure.active:
            state.failure.active = True
            state.failure.since = now
            state.failure.reason = reason
            state.failure.recovery_stable_since = None

            # Suspend manual lease if active
            if state.manual.active:
                state.manual.suspended = True
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.MANUAL_OVERRIDE_SUSPENDED.value,
                        severity=AuditSeverity.WARNING,
                        reason="Manual override suspended due to junction failure",
                    )
                )

            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.FAILURE_ENTERED.value,
                    severity=AuditSeverity.ERROR,
                    mode=Mode.FAILURE,
                    reason=f"Junction entered FAILURE: {reason}",
                )
            )

        # Drive fail-safe sequence
        cls._drive_fail_safe(state, actions, now, config)

    @classmethod
    def _drive_fail_safe(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        now: datetime,
        config: Optional[DefaultsConfig] = None,
    ) -> None:
        """Fail-safe sequence: any direction not confirmed RED or YELLOW is commanded YELLOW, then RED."""
        needs_yellow = []
        needs_red = []

        for d, d_state in state.directions.items():
            if d_state.actual in (SignalColor.GREEN, SignalColor.UNKNOWN):
                needs_yellow.append(d)
            elif d_state.actual == SignalColor.YELLOW:
                needs_red.append(d)
            elif d_state.actual == SignalColor.RED and d_state.desired != SignalColor.RED:
                needs_red.append(d)

        if needs_yellow:
            state.step = StepState(
                kind=StepKind.RECOVERY_YELLOW,
                phase=None,
                status=StepStatus.PENDING,
                started_at=now,
                confirmed_at=None,
            )
            for d in needs_yellow:
                cls._emit_command(state, d, SignalColor.YELLOW, None, actions, now)
        elif needs_red:
            # Invariant: A RED only after at least 5 s of confirmed yellow; yellow is never shortened in recovery
            if state.step.kind == StepKind.RECOVERY_YELLOW:
                if state.step.status != StepStatus.CONFIRMED or state.step.confirmed_at is None:
                    return
                yellow_dwell = (now - state.step.confirmed_at).total_seconds()
                req_yellow = config.timing.yellow_seconds if config else 5.0
                if yellow_dwell < req_yellow:
                    return

            state.step = StepState(
                kind=StepKind.RECOVERY_RED,
                phase=None,
                status=StepStatus.PENDING,
                started_at=now,
                confirmed_at=None,
            )
            for d in needs_red:
                cls._emit_command(state, d, SignalColor.RED, None, actions, now)
        else:
            # Everything confirmed RED: hold ALL_RED
            if state.step.kind not in (StepKind.ALL_RED, StepKind.RECOVERY_RED):
                state.step = StepState(
                    kind=StepKind.ALL_RED,
                    phase=None,
                    status=StepStatus.CONFIRMED,
                    started_at=now,
                    confirmed_at=now,
                )

    # -------------------------------------------------------------------------
    # Startup Handling (Section 18.6)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_startup(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        # Reset all actual states to UNKNOWN upon restart
        for d, d_state in state.directions.items():
            d_state.actual = SignalColor.UNKNOWN
            if d_state.pending_command:
                d_state.pending_command.status = CommandStatus.ABANDONED
                d_state.pending_command = None

        # Check manual lease
        if state.manual.active:
            if state.manual.expires_at and now >= state.manual.expires_at:
                state.manual.active = False
                state.manual.suspended = False
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.MANUAL_OVERRIDE_ENDED.value,
                        severity=AuditSeverity.INFO,
                        reason="Manual lease expired during downtime",
                    )
                )
            else:
                state.manual.suspended = True

        # Check emergency expiration during downtime (Spec 18.6: downtime counts)
        for v in list(state.vehicles.values()):
            if v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY:
                if (now - v.arrived_at_server).total_seconds() >= config.emergency.stale_timeout_seconds:
                    v.status = VehicleStatus.CLEARED
                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.EMERGENCY_TIMEOUT.value,
                            severity=AuditSeverity.WARNING,
                            vehicle_id=v.vehicle_id,
                            reason="Emergency vehicle expired during downtime (exceeded 120s)",
                        )
                    )

        actions.append(
            AuditRecord(
                junction_id=state.junction_id,
                timestamp=now,
                event_type=AuditEventType.RESTART_RECOVERY.value,
                severity=AuditSeverity.WARNING,
                reason="Application restarted; initiating fail-safe recovery to confirmed ALL_RED",
            )
        )

        cls._enter_failure(state, "Startup / restart recovery", actions, now, config)
        return EventOutcome.ACCEPTED, "Startup recovery initiated"

    # -------------------------------------------------------------------------
    # Sensor Events (Section 8)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_sensor_event(
        cls,
        state: JunctionState,
        event: SensorEventInput,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        # 1. Junction validation
        if event.junction_id != state.junction_id:
            return EventOutcome.REJECTED_UNKNOWN_JUNCTION, f"Unknown junction {event.junction_id}"

        # 2. Direction validation
        if event.direction not in state.directions:
            return EventOutcome.REJECTED_INVALID, f"Invalid direction {event.direction}"

        # 3. Timestamp sanity check (Section 8.5)
        if config.events.enforce_timestamp_window:
            age = (now - event.timestamp).total_seconds()
            if age > config.events.max_event_age_seconds:
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.SENSOR_EVENT_REJECTED.value,
                        severity=AuditSeverity.WARNING,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        reason=f"Event timestamp {event.timestamp.isoformat()} is too stale ({age:.1f}s > {config.events.max_event_age_seconds}s)",
                    )
                )
                return EventOutcome.REJECTED_STALE, "Event timestamp too stale"

            future_skew = (event.timestamp - now).total_seconds()
            if future_skew > config.events.max_future_skew_seconds:
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.SENSOR_EVENT_REJECTED.value,
                        severity=AuditSeverity.WARNING,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        reason=f"Event timestamp {event.timestamp.isoformat()} is in the future by {future_skew:.1f}s",
                    )
                )
                return EventOutcome.REJECTED_INVALID, "Event timestamp too far in the future"

        # 4. Duplicate event_id check (Idempotency)
        if event.event_id in state.processed_events:
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.SENSOR_EVENT_DUPLICATE.value,
                    severity=AuditSeverity.INFO,
                    direction=event.direction,
                    cause_event_id=event.event_id,
                    reason=f"Duplicate event_id '{event.event_id}' ignored",
                )
            )
            return EventOutcome.DUPLICATE_IGNORED, "Duplicate event ignored"

        # Evidence beats a flag: receiving sensor event marks direction sensor ONLINE (Section 8.6)
        if state.directions[event.direction].sensor_status != DeviceStatus.ONLINE:
            state.directions[event.direction].sensor_status = DeviceStatus.ONLINE
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.DEVICE_STATUS_INFERRED_ONLINE.value,
                    severity=AuditSeverity.INFO,
                    direction=event.direction,
                    reason=f"Sensor for {event.direction} inferred ONLINE from valid sensor event",
                )
            )

        # 5. Process vehicle arrival or clearance
        v_type = VehicleType.from_str(event.vehicle_type)
        existing_veh = state.vehicles.get(event.vehicle_id)

        outcome: EventOutcome
        msg: str

        if event.event_type == "VEHICLE_ARRIVED":
            if existing_veh:
                if existing_veh.status == VehicleStatus.WAITING:
                    outcome = EventOutcome.DUPLICATE_IGNORED
                    msg = f"Vehicle {event.vehicle_id} is already WAITING in queue"
                elif event.sequence_no <= existing_veh.last_sequence_no:
                    outcome = EventOutcome.IGNORED_OUT_OF_ORDER
                    msg = f"Out of order sequence {event.sequence_no} <= {existing_veh.last_sequence_no}"
                else:
                    # Re-arrival after being cleared
                    state.vehicles[event.vehicle_id] = VehicleRecord(
                        vehicle_id=event.vehicle_id,
                        vehicle_type=v_type,
                        direction=event.direction,
                        status=VehicleStatus.WAITING,
                        arrived_at_server=now,
                        arrived_at_sensor=event.timestamp,
                        last_sequence_no=event.sequence_no,
                    )
                    outcome = EventOutcome.ACCEPTED
                    msg = f"Vehicle {event.vehicle_id} arrived at {event.direction}"
            else:
                state.vehicles[event.vehicle_id] = VehicleRecord(
                    vehicle_id=event.vehicle_id,
                    vehicle_type=v_type,
                    direction=event.direction,
                    status=VehicleStatus.WAITING,
                    arrived_at_server=now,
                    arrived_at_sensor=event.timestamp,
                    last_sequence_no=event.sequence_no,
                )
                outcome = EventOutcome.ACCEPTED
                msg = f"Vehicle {event.vehicle_id} arrived at {event.direction}"

            if outcome == EventOutcome.ACCEPTED:
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.VEHICLE_DETECTED.value,
                        severity=AuditSeverity.INFO,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        vehicle_id=event.vehicle_id,
                        reason=f"Vehicle {event.vehicle_id} ({v_type.value}) detected on {event.direction}",
                    )
                )
                if v_type == VehicleType.EMERGENCY:
                    # Emergency preemption ends any active manual control lease
                    if state.manual.active:
                        state.manual.active = False
                        state.manual.target_phase = None
                        state.manual.expires_at = None
                        state.manual.suspended = False
                        actions.append(
                            AuditRecord(
                                junction_id=state.junction_id,
                                timestamp=now,
                                event_type=AuditEventType.MANUAL_OVERRIDE_ENDED.value,
                                severity=AuditSeverity.WARNING,
                                vehicle_id=event.vehicle_id,
                                reason=f"Emergency vehicle {event.vehicle_id} preempted and terminated manual lease",
                            )
                        )

                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.EMERGENCY_DETECTED.value,
                            severity=AuditSeverity.WARNING,
                            direction=event.direction,
                            cause_event_id=event.event_id,
                            vehicle_id=event.vehicle_id,
                            reason=f"EMERGENCY vehicle {event.vehicle_id} detected on {event.direction}",
                        )
                    )

        elif event.event_type == "VEHICLE_CLEARED":
            if not existing_veh:
                # CLEARED without ARRIVAL: store tombstone, do not modify queue (prevent negative queue)
                state.vehicles[event.vehicle_id] = VehicleRecord(
                    vehicle_id=event.vehicle_id,
                    vehicle_type=v_type,
                    direction=event.direction,
                    status=VehicleStatus.CLEARED,
                    arrived_at_server=now,
                    arrived_at_sensor=event.timestamp,
                    last_sequence_no=event.sequence_no,
                )
                outcome = EventOutcome.ACCEPTED_NO_CHANGE
                msg = f"CLEARED event for unseen vehicle {event.vehicle_id}; stored tombstone"
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.WARNING_RAISED.value,
                        severity=AuditSeverity.WARNING,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        vehicle_id=event.vehicle_id,
                        reason=f"CLEARED without arrival for vehicle {event.vehicle_id}",
                    )
                )
            elif existing_veh.status == VehicleStatus.CLEARED:
                outcome = EventOutcome.DUPLICATE_IGNORED
                msg = f"Vehicle {event.vehicle_id} was already CLEARED"
            elif event.sequence_no <= existing_veh.last_sequence_no:
                outcome = EventOutcome.IGNORED_OUT_OF_ORDER
                msg = f"Out of order sequence {event.sequence_no} <= {existing_veh.last_sequence_no}"
            elif existing_veh.direction != event.direction:
                outcome = EventOutcome.IGNORED
                msg = f"Direction mismatch: {event.direction} != {existing_veh.direction}"
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.WARNING_RAISED.value,
                        severity=AuditSeverity.WARNING,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        vehicle_id=event.vehicle_id,
                        reason=f"CLEARED direction {event.direction} differs from arrival {existing_veh.direction}",
                    )
                )
            else:
                existing_veh.status = VehicleStatus.CLEARED
                existing_veh.last_sequence_no = event.sequence_no
                outcome = EventOutcome.ACCEPTED
                msg = f"Vehicle {event.vehicle_id} cleared from {event.direction}"

                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.VEHICLE_CLEARED.value,
                        severity=AuditSeverity.INFO,
                        direction=event.direction,
                        cause_event_id=event.event_id,
                        vehicle_id=event.vehicle_id,
                        reason=f"Vehicle {event.vehicle_id} cleared from {event.direction}",
                    )
                )
                if existing_veh.vehicle_type == VehicleType.EMERGENCY:
                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.EMERGENCY_CLEARED.value,
                            severity=AuditSeverity.INFO,
                            direction=event.direction,
                            cause_event_id=event.event_id,
                            vehicle_id=event.vehicle_id,
                            reason=f"Emergency vehicle {event.vehicle_id} cleared",
                        )
                    )
        else:
            return EventOutcome.REJECTED_INVALID, f"Unknown event_type '{event.event_type}'"

        # Record processed event ID
        payload_hash = hashlib.sha256((event.raw_payload or "").encode()).hexdigest()
        state.processed_events[event.event_id] = ProcessedEventRecord(
            event_id=event.event_id,
            received_at=now,
            outcome=outcome,
            payload_hash=payload_hash,
        )

        return outcome, msg

    # -------------------------------------------------------------------------
    # Admin Commands (Section 13)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_admin_command(
        cls,
        state: JunctionState,
        cmd: AdminCommandInput,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        mode = compute_mode(state)

        if cmd.command == "MANUAL_GREEN_REQUEST":
            if mode == Mode.EMERGENCY:
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.ADMIN_COMMAND_REJECTED.value,
                        severity=AuditSeverity.WARNING,
                        reason="Manual green request rejected: EMERGENCY mode active",
                    )
                )
                return EventOutcome.REJECTED_INVALID, "EMERGENCY_ACTIVE"

            if mode == Mode.FAILURE:
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.ADMIN_COMMAND_REJECTED.value,
                        severity=AuditSeverity.WARNING,
                        reason="Manual green request rejected: Junction in FAILURE",
                    )
                )
                return EventOutcome.REJECTED_INVALID, "JUNCTION_IN_FAILURE"

            if not cmd.direction or cmd.direction not in state.directions:
                return EventOutcome.REJECTED_INVALID, f"Invalid direction '{cmd.direction}'"

            # Find phase containing requested direction
            target_phase = None
            for p_name, dirs in state.phases.items():
                if cmd.direction in dirs:
                    target_phase = p_name
                    break

            if not target_phase:
                return EventOutcome.REJECTED_INVALID, f"No phase found for direction {cmd.direction}"

            was_active = state.manual.active
            state.manual.active = True
            state.manual.target_phase = target_phase
            state.manual.expires_at = now + timedelta(seconds=config.manual.lease_seconds)
            state.manual.requested_by = cmd.requested_by
            state.manual.suspended = False

            event_type = (
                AuditEventType.MANUAL_OVERRIDE_RENEWED.value
                if was_active
                else AuditEventType.MANUAL_OVERRIDE_STARTED.value
            )
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=event_type,
                    severity=AuditSeverity.INFO,
                    mode=Mode.MANUAL,
                    reason=f"Manual green requested for {cmd.direction} (phase {target_phase}) by {cmd.requested_by or 'admin'}",
                )
            )
            return EventOutcome.ACCEPTED, "Manual override accepted"

        elif cmd.command == "RETURN_TO_AUTOMATIC":
            if state.manual.active:
                state.manual.active = False
                state.manual.target_phase = None
                state.manual.expires_at = None
                state.manual.suspended = False
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.RETURN_TO_AUTOMATIC.value,
                        severity=AuditSeverity.INFO,
                        reason=f"Manual override ended by {cmd.requested_by or 'admin'}, returned to AUTOMATIC",
                    )
                )
                return EventOutcome.ACCEPTED, "Returned to automatic"
            else:
                return EventOutcome.ACCEPTED_NO_CHANGE, "Already in automatic"

        return EventOutcome.REJECTED_INVALID, f"Unknown admin command '{cmd.command}'"

    # -------------------------------------------------------------------------
    # Controller Replies (Section 15)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_controller_reply(
        cls,
        state: JunctionState,
        reply: ControllerReplyInput,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        # Correlate command_id with direction's pending_command
        matched_dir: Optional[str] = None
        for d, d_state in state.directions.items():
            if d_state.pending_command and d_state.pending_command.command_id == reply.command_id:
                matched_dir = d
                break

        if not matched_dir:
            # Unmatched ACK / duplicate / superseded
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.ACK_IGNORED.value,
                    severity=AuditSeverity.WARNING,
                    command_id=reply.command_id,
                    reason=f"Controller reply for unmapped or already resolved command {reply.command_id}",
                )
            )
            return EventOutcome.DUPLICATE_IGNORED, "Reply unmapped or already resolved"

        dir_state = state.directions[matched_dir]
        pending_cmd = dir_state.pending_command
        assert pending_cmd is not None

        if reply.status == "ACK":
            reported_state = reply.actual_state or pending_cmd.requested_state
            if reported_state != pending_cmd.requested_state:
                # Wrong state reported in ACK! Counts as failed attempt
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.STATE_MISMATCH.value,
                        severity=AuditSeverity.ERROR,
                        direction=matched_dir,
                        command_id=reply.command_id,
                        previous_state=dir_state.actual,
                        new_state=reported_state,
                        reason=f"Controller ACK reported {reported_state.value}, but requested was {pending_cmd.requested_state.value}",
                    )
                )
                cls._enter_failure(state, f"State mismatch on {matched_dir}", actions, now)
                return EventOutcome.REJECTED_INVALID, "State mismatch in ACK"

            # Valid ACK
            dir_state.actual = reported_state
            dir_state.last_confirmed_at = now
            dir_state.signal_controller_status = DeviceStatus.ONLINE
            pending_cmd.status = CommandStatus.ACKED
            dir_state.pending_command = None

            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.SIGNAL_CHANGED.value,
                    severity=AuditSeverity.INFO,
                    direction=matched_dir,
                    command_id=reply.command_id,
                    new_state=reported_state,
                    mode=compute_mode(state),
                    reason=f"Controller confirmed {matched_dir} is now {reported_state.value}",
                )
            )

            # Check if current step is now CONFIRMED (all directions have actual == desired)
            step_confirmed = all(d.actual == d.desired for d in state.directions.values())
            if step_confirmed and state.step.status == StepStatus.PENDING:
                state.step.status = StepStatus.CONFIRMED
                state.step.confirmed_at = now

            return EventOutcome.ACCEPTED, "ACK processed"

        elif reply.status == "NACK":
            pending_cmd.status = CommandStatus.NACKED
            dir_state.signal_controller_status = DeviceStatus.DEGRADED
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.COMMAND_FAILED.value,
                    severity=AuditSeverity.ERROR,
                    direction=matched_dir,
                    command_id=reply.command_id,
                    reason=f"Controller sent NACK for command {reply.command_id} on {matched_dir}",
                )
            )
            cls._enter_failure(state, f"Controller NACK on {matched_dir}", actions, now)
            return EventOutcome.ACCEPTED, "NACK recorded"

        return EventOutcome.REJECTED_INVALID, f"Unknown reply status '{reply.status}'"

    # -------------------------------------------------------------------------
    # Device Status Events (Section 16.10)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_device_status(
        cls,
        state: JunctionState,
        event: DeviceStatusInput,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        dirs = [event.direction] if event.direction else list(state.directions.keys())

        for d in dirs:
            if d not in state.directions:
                continue
            dir_state = state.directions[d]

            if event.device_type == DeviceType.SIGNAL_CONTROLLER:
                old_status = dir_state.signal_controller_status
                dir_state.signal_controller_status = event.status
                dir_state.last_status_timestamp = event.timestamp

                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.DEVICE_STATUS_CHANGED.value,
                        severity=AuditSeverity.WARNING if event.status != DeviceStatus.ONLINE else AuditSeverity.INFO,
                        direction=d,
                        reason=f"SIGNAL_CONTROLLER on {d} changed status from {old_status.value} to {event.status.value}",
                    )
                )

                if event.status in (DeviceStatus.OFFLINE, DeviceStatus.UNKNOWN):
                    cls._enter_failure(state, f"Signal controller on {d} reported {event.status.value}", actions, now)

            elif event.device_type == DeviceType.SENSOR:
                old_status = dir_state.sensor_status
                dir_state.sensor_status = event.status
                dir_state.last_status_timestamp = event.timestamp

                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.DEVICE_STATUS_CHANGED.value,
                        severity=AuditSeverity.WARNING if event.status != DeviceStatus.ONLINE else AuditSeverity.INFO,
                        direction=d,
                        reason=f"SENSOR on {d} changed status from {old_status.value} to {event.status.value}",
                    )
                )

        return EventOutcome.ACCEPTED, "Device status updated"

    # -------------------------------------------------------------------------
    # Tick Handling (Section 14.4)
    # -------------------------------------------------------------------------
    @classmethod
    def _handle_tick(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> Tuple[EventOutcome, str]:
        # 1. Housekeeping: expire ghost vehicles and stale emergencies
        cls._cleanup_expired_vehicles(state, actions, config, now)

        # 2. Check manual lease expiry
        if state.manual.active and state.manual.expires_at and now >= state.manual.expires_at:
            state.manual.active = False
            state.manual.target_phase = None
            state.manual.expires_at = None
            state.manual.suspended = False
            actions.append(
                AuditRecord(
                    junction_id=state.junction_id,
                    timestamp=now,
                    event_type=AuditEventType.MANUAL_OVERRIDE_ENDED.value,
                    severity=AuditSeverity.INFO,
                    reason="Manual override lease expired; returning to AUTOMATIC",
                )
            )

        # 3. Check pending controller command timeouts & retries (Section 15.7)
        cls._check_command_timeouts(state, actions, config, now)

        # 4. If in FAILURE, check recovery stability (Section 16.9)
        if state.failure.active:
            cls._check_failure_recovery(state, actions, config, now)
            return EventOutcome.ACCEPTED, "Tick: failure recovery active"

        # 5. Heartbeat state refresh (Section 15.8)
        cls._check_state_refresh(state, actions, config, now)

        # 6. Step transition advancement (Section 5.2)
        cls._advance_step_transitions(state, actions, config, now)

        return EventOutcome.ACCEPTED, "Tick completed"

    # -------------------------------------------------------------------------
    # Sub-routines for Tick
    # -------------------------------------------------------------------------
    @classmethod
    def _cleanup_expired_vehicles(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> None:
        # Ghost vehicles (non-emergency waiting > vehicle_ghost_expiry_seconds)
        for v in list(state.vehicles.values()):
            if v.status == VehicleStatus.WAITING and v.vehicle_type != VehicleType.EMERGENCY:
                waited = (now - v.arrived_at_server).total_seconds()
                if waited >= config.events.vehicle_ghost_expiry_seconds:
                    v.status = VehicleStatus.CLEARED
                    if v.direction in state.directions:
                        state.directions[v.direction].sensor_status = DeviceStatus.WARNING
                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.VEHICLE_EXPIRED.value,
                            severity=AuditSeverity.WARNING,
                            direction=v.direction,
                            vehicle_id=v.vehicle_id,
                            reason=f"Non-emergency vehicle {v.vehicle_id} expired after {waited:.1f}s",
                        )
                    )

        # Stale emergencies
        _, audit_recs = EmergencyHandler.clean_stale_emergencies(state, now, config)
        actions.extend(audit_recs)

    @classmethod
    def _check_command_timeouts(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> None:
        for d, d_state in state.directions.items():
            cmd = d_state.pending_command
            if not cmd or cmd.status != CommandStatus.PENDING:
                continue

            elapsed = (now - cmd.sent_at).total_seconds()
            if elapsed >= config.controller.ack_timeout_seconds:
                if cmd.attempt <= config.controller.max_retries:
                    # Retry
                    cmd.attempt += 1
                    cmd.sent_at = now
                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.COMMAND_RETRY.value,
                            severity=AuditSeverity.WARNING,
                            direction=d,
                            command_id=cmd.command_id,
                            reason=f"ACK timeout on {d} (elapsed {elapsed:.1f}s); retry attempt {cmd.attempt}",
                        )
                    )
                    actions.append(SendSignalCommandAction(cmd))
                else:
                    # Retries exhausted: FAILED
                    cmd.status = CommandStatus.FAILED
                    d_state.actual = SignalColor.UNKNOWN
                    d_state.pending_command = None
                    # Spec: Direction actual becomes UNKNOWN; OFFLINE comes ONLY from explicit status events

                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.COMMAND_FAILED.value,
                            severity=AuditSeverity.ERROR,
                            direction=d,
                            command_id=cmd.command_id,
                            reason=f"No ACK received for {cmd.command_id} on {d} after {cmd.attempt} attempts",
                        )
                    )
                    cls._enter_failure(state, f"Command {cmd.command_id} on {d} failed to get ACK", actions, now, config)

    @classmethod
    def _check_failure_recovery(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> None:
        """Section 16.9: Exits FAILURE when all-red is confirmed and stable for recovery_stable_seconds."""
        all_controllers_online = all(
            d.signal_controller_status == DeviceStatus.ONLINE for d in state.directions.values()
        )
        all_confirmed_red = all(d.actual == SignalColor.RED for d in state.directions.values())

        if all_controllers_online and all_confirmed_red:
            if state.failure.recovery_stable_since is None:
                state.failure.recovery_stable_since = now
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.RECOVERY_STABLE.value,
                        severity=AuditSeverity.INFO,
                        reason="All signals confirmed RED and controllers ONLINE; starting 10s stability timer",
                    )
                )
            else:
                stable_elapsed = (now - state.failure.recovery_stable_since).total_seconds()
                if stable_elapsed >= config.controller.recovery_stable_seconds:
                    # Shift waiting vehicles arrival time forward by failure downtime so stale timers are not penalized
                    if state.failure.since:
                        downtime = now - state.failure.since
                        for v in state.vehicles.values():
                            if v.status == VehicleStatus.WAITING:
                                v.arrived_at_server = v.arrived_at_server + downtime

                    # Successfully recover from failure!
                    state.failure.active = False
                    state.failure.since = None
                    state.failure.reason = None
                    state.failure.recovery_stable_since = None

                    # If manual was suspended, resume it
                    if state.manual.active and state.manual.suspended:
                        state.manual.suspended = False
                        actions.append(
                            AuditRecord(
                                junction_id=state.junction_id,
                                timestamp=now,
                                event_type=AuditEventType.MANUAL_OVERRIDE_RESUMED.value,
                                severity=AuditSeverity.INFO,
                                reason="Manual override resumed after recovery",
                            )
                        )

                    actions.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type=AuditEventType.FAILURE_EXITED.value,
                            severity=AuditSeverity.INFO,
                            reason="Failure recovery complete; returning to normal operation",
                        )
                    )

                    # Begin regular ALL_RED dwell before first green
                    state.step = StepState(
                        kind=StepKind.ALL_RED,
                        phase=None,
                        status=StepStatus.CONFIRMED,
                        started_at=now,
                        confirmed_at=now,
                    )
        else:
            # Flapping restarts the timer
            if state.failure.recovery_stable_since is not None:
                state.failure.recovery_stable_since = None
                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.WARNING_RAISED.value,
                        severity=AuditSeverity.WARNING,
                        reason="Controllers flapped or signals left RED during recovery; restarting stability timer",
                    )
                )
            cls._drive_fail_safe(state, actions, now)

    @classmethod
    def _check_state_refresh(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> None:
        """Section 15.8: Heartbeat state refresh while idle on confirmed green."""
        if state.step.kind != StepKind.GREEN or state.step.status != StepStatus.CONFIRMED:
            return

        for d, d_state in state.directions.items():
            if d_state.pending_command is not None:
                continue
            last_time = d_state.last_confirmed_at or state.step.confirmed_at or now
            if (now - last_time).total_seconds() >= config.controller.state_refresh_seconds:
                # Re-send current desired state
                cls._emit_command(state, d, d_state.desired, state.step.phase, actions, now)

    @classmethod
    def _advance_step_transitions(
        cls,
        state: JunctionState,
        actions: List[EngineAction],
        config: DefaultsConfig,
        now: datetime,
    ) -> None:
        """Advances the state machine: GREEN -> YELLOW -> ALL_RED -> GREEN."""
        # Dwell timers only run while step is CONFIRMED (Section 5.7)
        if state.step.status != StepStatus.CONFIRMED or state.step.confirmed_at is None:
            return

        dwell = (now - state.step.confirmed_at).total_seconds()
        mode = compute_mode(state)

        # -------------------------------------------------------------
        # 1. In YELLOW(P): transition to ALL_RED after yellow_seconds
        # -------------------------------------------------------------
        if state.step.kind == StepKind.YELLOW:
            if dwell >= config.timing.yellow_seconds:
                state.transition_counter += 1
                tr_id = f"tr-{state.junction_id}-{state.transition_counter}"

                # Send RED to current yellow directions
                state.step = StepState(
                    kind=StepKind.ALL_RED,
                    phase=None,
                    status=StepStatus.PENDING,
                    started_at=now,
                    confirmed_at=None,
                )
                for d in state.directions:
                    if state.directions[d].desired != SignalColor.RED:
                        cls._emit_command(state, d, SignalColor.RED, None, actions, now, transition_id=tr_id)

        # -------------------------------------------------------------
        # 2. In ALL_RED: choose target and transition to GREEN
        # -------------------------------------------------------------
        elif state.step.kind == StepKind.ALL_RED:
            if dwell >= config.timing.all_red_seconds:
                # Target evaluation at the end of ALL_RED: Emergency > Manual > Next Target > Scheduler
                final_target: Optional[str] = None

                # Check Emergency
                emergency_target = EmergencyHandler.determine_emergency_target(state, now, config)
                if emergency_target:
                    final_target = emergency_target
                    state.emergency.serving_phase = emergency_target
                    state.emergency.hold_started_at = now
                elif state.manual.active and state.manual.target_phase:
                    final_target = state.manual.target_phase
                elif state.next_target:
                    final_target = state.next_target
                    state.next_target = None
                else:
                    # Pick highest scoring phase or first phase
                    scores = TrafficScheduler.calculate_phase_scores(state, now, config)
                    if scores:
                        final_target = max(scores.keys(), key=lambda p: scores[p])
                    else:
                        final_target = next(iter(state.phases.keys()))

                if not final_target or final_target not in state.phases:
                    final_target = next(iter(state.phases.keys()))

                # Switch to GREEN(final_target)
                state.transition_counter += 1
                tr_id = f"tr-{state.junction_id}-{state.transition_counter}"

                state.step = StepState(
                    kind=StepKind.GREEN,
                    phase=final_target,
                    status=StepStatus.PENDING,
                    started_at=now,
                    confirmed_at=None,
                )

                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.SIGNAL_TRANSITION_STARTED.value,
                        severity=AuditSeverity.INFO,
                        mode=mode,
                        transition_id=tr_id,
                        reason=f"Transitioning to GREEN phase {final_target}",
                    )
                )

                # Send GREEN commands for target phase directions
                target_dirs = state.phases[final_target]
                for d in target_dirs:
                    cls._emit_command(state, d, SignalColor.GREEN, final_target, actions, now, transition_id=tr_id)

        # -------------------------------------------------------------
        # 3. In GREEN(P): evaluate switch to YELLOW
        # -------------------------------------------------------------
        elif state.step.kind == StepKind.GREEN:
            current_phase = state.step.phase
            assert current_phase is not None

            next_phase: Optional[str] = None

            # Emergency Preemption (Section 12.2)
            emergency_target = EmergencyHandler.determine_emergency_target(state, now, config)
            if emergency_target:
                if emergency_target != current_phase:
                    if dwell >= config.timing.green_min_emergency_seconds:
                        next_phase = emergency_target
                else:
                    # Hold green for emergency
                    return

            # Manual control switch
            elif mode == Mode.MANUAL:
                if state.manual.target_phase and state.manual.target_phase != current_phase:
                    if dwell >= config.timing.green_min_seconds:
                        next_phase = state.manual.target_phase

            # Degraded round-robin
            elif mode == Mode.DEGRADED:
                next_phase = TrafficScheduler.evaluate_degraded_switch(state, now, config)

            # Automatic scheduling
            elif mode == Mode.AUTOMATIC:
                next_phase = TrafficScheduler.evaluate_automatic_switch(state, now, config)

            # Initiate transition to YELLOW(current_phase)
            if next_phase:
                state.transition_counter += 1
                tr_id = f"tr-{state.junction_id}-{state.transition_counter}"
                state.next_target = next_phase

                state.step = StepState(
                    kind=StepKind.YELLOW,
                    phase=current_phase,
                    status=StepStatus.PENDING,
                    started_at=now,
                    confirmed_at=None,
                )

                actions.append(
                    AuditRecord(
                        junction_id=state.junction_id,
                        timestamp=now,
                        event_type=AuditEventType.SIGNAL_TRANSITION_STARTED.value,
                        severity=AuditSeverity.INFO,
                        mode=mode,
                        transition_id=tr_id,
                        reason=f"Switching from {current_phase} to {next_phase}; commanding YELLOW on {current_phase}",
                    )
                )

                current_dirs = state.phases[current_phase]
                for d in current_dirs:
                    cls._emit_command(state, d, SignalColor.YELLOW, None, actions, now, transition_id=tr_id)
