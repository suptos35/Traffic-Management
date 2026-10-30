import asyncio
from datetime import datetime, timezone
import os
import tempfile
import pytest
from backend.adapters.persistence.repository import SQLiteRepository
from backend.application.junction_worker import JunctionWorker
from backend.config.config import AppConfig
from backend.domain import (
    AdminCommandInput,
    ControllerReplyInput,
    EventOutcome,
    Mode,
    SensorEventInput,
    SignalColor,
    StepKind,
    VehicleStatus,
    compute_mode,
)


@pytest.fixture
async def worker():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app_cfg = AppConfig.load_from_yaml("config/default_config.yaml")
    repo = SQLiteRepository(db_path=path)
    w = JunctionWorker("A", app_cfg, repo)
    await w.start()
    yield w
    await w.stop()
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.asyncio
async def test_pdf_concurrent_sequence(worker: JunctionWorker):
    """PDF Section 15 Scenario 9: Events affecting the same junction occur almost simultaneously:

    T = 0 ms: NORTH truck arrives
    T = 4 ms: EAST emergency arrives
    T = 8 ms: Administrator requests WEST manual control
    T = 12 ms: Duplicate EAST emergency event arrives
    T = 17 ms: Controller ACK arrives
    """
    now = datetime.now(timezone.utc)
    from backend.domain.types import DeviceStatus, SignalColor, StepKind, StepState, StepStatus
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

    # 1. NORTH truck arrives
    in1 = SensorEventInput("evt-tr-1", "A", "NORTH", "VEHICLE_ARRIVED", "TRUCK-99", 1, now, "TRUCK")

    # 2. EAST emergency arrives
    in2 = SensorEventInput("evt-em-1", "A", "EAST", "VEHICLE_ARRIVED", "AMB-99", 1, now, "EMERGENCY")

    # 3. Admin requests WEST manual control
    in3 = AdminCommandInput(command="MANUAL_GREEN_REQUEST", direction="WEST", requested_by="admin-user")

    # 4. Duplicate EAST emergency event arrives
    in4 = SensorEventInput("evt-em-1", "A", "EAST", "VEHICLE_ARRIVED", "AMB-99", 1, now, "EMERGENCY")

    # Fire all 4 concurrently
    results = await asyncio.gather(
        worker.submit(in1),
        worker.submit(in2),
        worker.submit(in3),
        worker.submit(in4),
        return_exceptions=True,
    )

    outcomes = [r[0] if isinstance(r, tuple) else type(r) for r in results]

    # in1 (Truck) accepted
    assert outcomes[0] == EventOutcome.ACCEPTED

    # in2 (Emergency) accepted
    assert outcomes[1] == EventOutcome.ACCEPTED

    # in3 (Manual) rejected because emergency was accepted
    assert outcomes[2] == EventOutcome.REJECTED_INVALID

    # in4 (Duplicate) ignored
    assert outcomes[3] == EventOutcome.DUPLICATE_IGNORED

    # Junction must be in EMERGENCY mode
    assert compute_mode(worker.state) == Mode.EMERGENCY

    # Queue state must be exactly 1 TRUCK and 1 EMERGENCY
    waiting_vehicles = [v for v in worker.state.vehicles.values() if v.status == VehicleStatus.WAITING]
    assert len(waiting_vehicles) == 2
    assert {v.vehicle_id for v in waiting_vehicles} == {"TRUCK-99", "AMB-99"}
