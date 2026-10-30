from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from backend.adapters.http.schemas import (
    AdminCommandRequest,
    AdminCommandResponse,
    AlertDetail,
    ControllerBehaviorRequest,
    ControllerEventRequest,
    CreateJunctionRequest,
    DeviceEventRequest,
    EmergencyDetail,
    FailureDetail,
    HistoryResponse,
    JunctionStatusResponse,
    JunctionSummaryItem,
    ManualDetail,
    SensorEventRequest,
    SensorEventResponse,
    StepDetail,
)
from backend.application.junction_worker import InboxFullError
from backend.application.manager import JunctionManager
from backend.config.config import JunctionDefinition, PhaseDefinition
from backend.domain.engine import compute_mode
from backend.domain.types import (
    AdminCommandInput,
    AuditSeverity,
    ControllerReplyInput,
    DeviceStatus,
    DeviceStatusInput,
    DeviceType,
    EventOutcome,
    Mode,
    SensorEventInput,
    SignalColor,
    VehicleStatus,
    VehicleType,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["Traffic API"])


def get_manager() -> JunctionManager:
    # Will be injected from app.state
    from backend.main import app_manager
    return app_manager


@router.get("/health", summary="Health check endpoint")
async def health_check(mgr: JunctionManager = Depends(get_manager)):
    return {
        "status": "HEALTHY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "active_junctions": mgr.list_junction_ids(),
    }


# ---------------------------------------------------------
# Junctions Management (Section 10.1)
# ---------------------------------------------------------
@router.get("/junctions", response_model=List[JunctionSummaryItem], summary="List all junctions")
async def list_junctions(mgr: JunctionManager = Depends(get_manager)):
    results: List[JunctionSummaryItem] = []
    for j_id in mgr.list_junction_ids():
        worker = mgr.get_worker(j_id)
        if not worker or not worker.state:
            continue
        state = worker.state

        desired = {d: s.desired.value for d, s in state.directions.items()}
        actual = {d: s.actual.value for d, s in state.directions.items()}
        queues = {
            d: sum(1 for v in state.vehicles.values() if v.direction == d and v.status == VehicleStatus.WAITING)
            for d in state.directions
        }

        # Controller & sensor status roll-up
        controller_stat = "ONLINE"
        sensor_stat = "ONLINE"
        for d in state.directions.values():
            if d.signal_controller_status in (DeviceStatus.OFFLINE, DeviceStatus.UNKNOWN):
                controller_stat = d.signal_controller_status.value
            if d.sensor_status in (DeviceStatus.OFFLINE, DeviceStatus.UNKNOWN):
                sensor_stat = d.sensor_status.value

        results.append(
            JunctionSummaryItem(
                junction_id=j_id,
                mode=compute_mode(state).value,
                phase=state.step.phase,
                controller_status=controller_stat,
                sensor_status=sensor_stat,
                desired_signals=desired,
                actual_signals=actual,
                queues=queues,
                emergency_active=any(
                    v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY
                    for v in state.vehicles.values()
                ),
                failure_active=state.failure.active,
                alerts_count=1 if state.failure.active else 0,
            )
        )
    return results


@router.post("/junctions", status_code=status.HTTP_201_CREATED, summary="Dynamically create a new junction")
async def create_junction(req: CreateJunctionRequest, mgr: JunctionManager = Depends(get_manager)):
    if mgr.get_worker(req.junction_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Junction '{req.junction_id}' already exists")

    try:
        phases = [PhaseDefinition(name=p["name"], directions=p["directions"]) for p in req.phases]
        definition = JunctionDefinition(
            directions=req.directions,
            phases=phases,
            overrides=req.overrides or {},
        )
        await mgr.register_junction(req.junction_id, definition)
        return {"status": "CREATED", "junction_id": req.junction_id}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/junctions/{junction_id}", summary="Get junction configuration definition")
async def get_junction_definition(junction_id: str, mgr: JunctionManager = Depends(get_manager)):
    worker = mgr.get_worker(junction_id)
    if not worker or not worker.state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{junction_id}' not found")

    return {
        "junction_id": junction_id,
        "directions": list(worker.state.directions.keys()),
        "phases": [{"name": p, "directions": dirs} for p, dirs in worker.state.phases.items()],
    }


# ---------------------------------------------------------
# Junction Status (Section 10.3 & 19.5)
# ---------------------------------------------------------
@router.get("/junctions/{junction_id}/status", response_model=JunctionStatusResponse, summary="Get full live junction state")
async def get_junction_status(junction_id: str, mgr: JunctionManager = Depends(get_manager)):
    worker = mgr.get_worker(junction_id)
    if not worker or not worker.state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{junction_id}' not found")

    state = worker.state
    now = datetime.now(timezone.utc)
    mode = compute_mode(state)

    desired = {d: s.desired.value for d, s in state.directions.items()}
    actual = {d: s.actual.value for d, s in state.directions.items()}
    queues = {
        d: sum(1 for v in state.vehicles.values() if v.direction == d and v.status == VehicleStatus.WAITING)
        for d in state.directions
    }

    # Devices
    devices = {
        d: {
            "signal_controller": s.signal_controller_status.value,
            "sensor": s.sensor_status.value,
        }
        for d, s in state.directions.items()
    }

    # Controller status roll-up
    controller_status = "ONLINE"
    sensor_status = "ONLINE"
    for s in state.directions.values():
        if s.signal_controller_status in (DeviceStatus.OFFLINE, DeviceStatus.UNKNOWN, DeviceStatus.DEGRADED):
            controller_status = s.signal_controller_status.value
        if s.sensor_status in (DeviceStatus.OFFLINE, DeviceStatus.UNKNOWN, DeviceStatus.DEGRADED):
            sensor_status = s.sensor_status.value

    # Pending commands
    pending_commands = []
    for d, s in state.directions.items():
        if s.pending_command:
            pending_commands.append({
                "command_id": s.pending_command.command_id,
                "direction": d,
                "requested_state": s.pending_command.requested_state.value,
                "attempt": s.pending_command.attempt,
                "sent_at": s.pending_command.sent_at.isoformat(),
            })

    # Emergency detail
    em_vehicles = [
        {
            "vehicle_id": v.vehicle_id,
            "direction": v.direction,
            "waiting_seconds": (now - v.arrived_at_server).total_seconds(),
        }
        for v in state.vehicles.values()
        if v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY
    ]

    # All waiting vehicles (for frontend clearance dropdown)
    waiting_vehicles_list = [
        {
            "vehicle_id": v.vehicle_id,
            "vehicle_type": v.vehicle_type.value,
            "direction": v.direction,
            "waiting_seconds": round((now - v.arrived_at_server).total_seconds(), 1),
        }
        for v in state.vehicles.values()
        if v.status == VehicleStatus.WAITING
    ]

    emergency_detail = EmergencyDetail(
        active=len(em_vehicles) > 0,
        vehicles=em_vehicles,
        serving_phase=state.emergency.serving_phase,
        hold_started_at=state.emergency.hold_started_at,
    )

    # Manual detail
    seconds_remaining = None
    if state.manual.active and state.manual.expires_at:
        seconds_remaining = max(0.0, (state.manual.expires_at - now).total_seconds())

    manual_detail = ManualDetail(
        active=state.manual.active,
        suspended=state.manual.suspended,
        target_phase=state.manual.target_phase,
        expires_at=state.manual.expires_at,
        seconds_remaining=seconds_remaining,
        requested_by=state.manual.requested_by,
    )

    # Failure detail
    failure_detail = FailureDetail(
        active=state.failure.active,
        since=state.failure.since,
        reason=state.failure.reason,
        recovery_stable_since=state.failure.recovery_stable_since,
    )

    # Active alerts list
    alerts: List[AlertDetail] = []
    if state.failure.active:
        alerts.append(
            AlertDetail(
                code="JUNCTION_IN_FAILURE",
                severity="ERROR",
                since=state.failure.since or now,
                message=f"Junction in FAILURE: {state.failure.reason}",
            )
        )
    if emergency_detail.active:
        alerts.append(
            AlertDetail(
                code="EMERGENCY_ACTIVE",
                severity="WARNING",
                since=now,
                message=f"Emergency preemption active ({len(em_vehicles)} emergency vehicles waiting)",
            )
        )
    for d, s in state.directions.items():
        if s.actual != s.desired and s.actual != SignalColor.UNKNOWN:
            alerts.append(
                AlertDetail(
                    code="STATE_MISMATCH",
                    severity="WARNING",
                    direction=d,
                    since=now,
                    message=f"Direction {d} desired={s.desired.value} but actual={s.actual.value}",
                )
            )
        if s.signal_controller_status != DeviceStatus.ONLINE:
            alerts.append(
                AlertDetail(
                    code=f"CONTROLLER_{s.signal_controller_status.value}",
                    severity="ERROR" if s.signal_controller_status == DeviceStatus.OFFLINE else "WARNING",
                    direction=d,
                    since=s.last_status_timestamp or now,
                    message=f"Signal controller on {d} is {s.signal_controller_status.value}",
                )
            )
        if s.sensor_status != DeviceStatus.ONLINE:
            alerts.append(
                AlertDetail(
                    code=f"SENSOR_{s.sensor_status.value}",
                    severity="WARNING",
                    direction=d,
                    since=s.last_status_timestamp or now,
                    message=f"Sensor on {d} is {s.sensor_status.value}",
                )
            )

    # Estimate phase ends at
    phase_ends_at = None
    if state.step.confirmed_at:
        if state.step.kind == "YELLOW":
            from datetime import timedelta
            phase_ends_at = state.step.confirmed_at + timedelta(seconds=worker.config.timing.yellow_seconds)
        elif state.step.kind == "ALL_RED":
            from datetime import timedelta
            phase_ends_at = state.step.confirmed_at + timedelta(seconds=worker.config.timing.all_red_seconds)

    step_detail = StepDetail(
        kind=state.step.kind.value,
        phase=state.step.phase,
        status=state.step.status.value,
        started_at=state.step.started_at,
        confirmed_at=state.step.confirmed_at,
        phase_ends_at=phase_ends_at,
        next_target=state.next_target,
    )

    return JunctionStatusResponse(
        junction_id=junction_id,
        server_time=now,
        mode=mode.value,
        phase=state.step.phase,
        step=step_detail,
        controller_status=controller_status,
        sensor_status=sensor_status,
        desired_signals=desired,
        actual_signals=actual,
        queues=queues,
        emergency=emergency_detail,
        manual=manual_detail,
        failure=failure_detail,
        devices=devices,
        pending_commands=pending_commands,
        alerts=alerts,
        waiting_vehicles=waiting_vehicles_list,
    )


# ---------------------------------------------------------
# Sensor Events (Section 10.2)
# ---------------------------------------------------------
@router.post("/sensor-events", response_model=SensorEventResponse, summary="Submit vehicle arrival or clearance event")
async def submit_sensor_event(req: SensorEventRequest, mgr: JunctionManager = Depends(get_manager)):
    worker = mgr.get_worker(req.junction_id)
    if not worker or not worker.state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{req.junction_id}' not found")

    inp = SensorEventInput(
        event_id=req.event_id,
        junction_id=req.junction_id,
        direction=req.direction,
        event_type=req.event_type,
        vehicle_id=req.vehicle_id,
        sequence_no=req.sequence_no,
        timestamp=req.timestamp,
        vehicle_type=req.vehicle_type,
        raw_payload=req.model_dump_json(),
    )

    try:
        outcome, msg = await worker.submit(inp)
    except InboxFullError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Junction inbox is full")
    except TimeoutError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Request timed out")

    if outcome == EventOutcome.REJECTED_STALE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Rejected: {msg}")
    elif outcome == EventOutcome.REJECTED_INVALID:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Rejected: {msg}")
    elif outcome == EventOutcome.REJECTED_UNKNOWN_JUNCTION:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Rejected: {msg}")

    # Return 200 for ACCEPTED, DUPLICATE_IGNORED, IGNORED_OUT_OF_ORDER (idempotent retry safe!)
    queues = {
        d: sum(1 for v in worker.state.vehicles.values() if v.direction == d and v.status == VehicleStatus.WAITING)
        for d in worker.state.directions
    }
    return SensorEventResponse(
        outcome=outcome.value,
        event_id=req.event_id,
        junction_id=req.junction_id,
        queues=queues,
        message=msg,
    )


# ---------------------------------------------------------
# Admin Commands (Section 10.4)
# ---------------------------------------------------------
@router.post("/junctions/{junction_id}/commands", response_model=AdminCommandResponse, status_code=status.HTTP_202_ACCEPTED, summary="Submit admin manual control command")
async def submit_admin_command(
    junction_id: str,
    req: AdminCommandRequest,
    mgr: JunctionManager = Depends(get_manager),
):
    worker = mgr.get_worker(junction_id)
    if not worker or not worker.state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{junction_id}' not found")

    inp = AdminCommandInput(
        command=req.command,
        direction=req.direction,
        requested_by=req.requested_by,
    )

    try:
        outcome, msg = await worker.submit(inp)
    except InboxFullError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Junction inbox is full")
    except TimeoutError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Request timed out")

    if outcome == EventOutcome.REJECTED_INVALID:
        if msg in ("EMERGENCY_ACTIVE", "JUNCTION_IN_FAILURE"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Conflict: {msg}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid: {msg}")

    return AdminCommandResponse(
        outcome=outcome.value,
        mode=compute_mode(worker.state).value,
        manual_expires_at=worker.state.manual.expires_at,
        message=msg,
    )


# ---------------------------------------------------------
# Controller Replies & Device Events (Section 10.5 & 19.8)
# ---------------------------------------------------------
@router.post("/controller-events", summary="Simulate or deliver physical controller ACK/NACK")
async def submit_controller_event(req: ControllerEventRequest, mgr: JunctionManager = Depends(get_manager)):
    # Find matching junction worker
    worker = None
    if req.junction_id:
        worker = mgr.get_worker(req.junction_id)
    else:
        # Search all junction workers for command_id
        for w in mgr.workers.values():
            if any(
                s.pending_command and s.pending_command.command_id == req.command_id
                for s in w.state.directions.values()
            ):
                worker = w
                break

    if not worker:
        # If not active in any worker, could be already resolved or unmapped
        return {"status": "UNMAPPED", "command_id": req.command_id}

    actual_col = SignalColor(req.actual_state) if req.actual_state else None
    inp = ControllerReplyInput(
        command_id=req.command_id,
        status=req.status.upper(),
        actual_state=actual_col,
        junction_id=req.junction_id,
        timestamp=req.timestamp or datetime.now(timezone.utc),
    )

    outcome, msg = await worker.submit(inp)
    return {"outcome": outcome.value, "command_id": req.command_id, "message": msg}


@router.post("/device-events", summary="Submit device status event (controller or sensor OFFLINE/ONLINE)")
async def submit_device_event(req: DeviceEventRequest, mgr: JunctionManager = Depends(get_manager)):
    worker = mgr.get_worker(req.junction_id)
    if not worker or not worker.state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{req.junction_id}' not found")

    inp = DeviceStatusInput(
        event_id=req.event_id,
        junction_id=req.junction_id,
        device_type=DeviceType(req.device_type.upper()),
        status=DeviceStatus(req.status.upper()),
        timestamp=req.timestamp,
        direction=req.direction,
    )

    outcome, msg = await worker.submit(inp)
    return {"outcome": outcome.value, "event_id": req.event_id, "message": msg}


# ---------------------------------------------------------
# History / Audit Log (Section 10.6)
# ---------------------------------------------------------
@router.get("/junctions/{junction_id}/history", response_model=HistoryResponse, summary="Query audit log history with cursor paging")
async def get_junction_history(
    junction_id: str,
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = Query(None),
    event_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    mgr: JunctionManager = Depends(get_manager),
):
    worker = mgr.get_worker(junction_id)
    if not worker:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{junction_id}' not found")

    items, next_cursor = mgr.repository.query_audit_log(
        junction_id=junction_id,
        limit=limit,
        before_id=before_id,
        event_type=event_type,
        severity=severity,
    )
    return HistoryResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------
# Simulator Controls (Section 14.8 & 19.10)
# ---------------------------------------------------------
@router.post("/simulator/controller-behavior", summary="Configure simulator controller behavior (AUTO_ACK, HOLD, DROP, WRONG_STATE, NACK)")
async def set_simulator_behavior(req: ControllerBehaviorRequest, mgr: JunctionManager = Depends(get_manager)):
    worker = mgr.get_worker(req.junction_id)
    if not worker:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Junction '{req.junction_id}' not found")

    worker.controller.set_behavior(req.behavior)
    if req.auto_ack_delay_ms is not None:
        worker.controller.auto_ack_delay_ms = req.auto_ack_delay_ms

    return {
        "status": "UPDATED",
        "junction_id": req.junction_id,
        "behavior": worker.controller.behavior,
        "auto_ack_delay_ms": worker.controller.auto_ack_delay_ms,
    }
