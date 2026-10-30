import asyncio
from datetime import datetime, timezone
import os
import tempfile
import pytest
from httpx import ASGITransport, AsyncClient
from backend.adapters.persistence.repository import SQLiteRepository
from backend.application.manager import JunctionManager
import backend.main as main_mod
from backend.config.config import AppConfig


@pytest.fixture
async def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    app_cfg = AppConfig.load_from_yaml("config/default_config.yaml")
    app_cfg.persistence.db_path = path
    repo = SQLiteRepository(db_path=path)
    mgr = JunctionManager(app_cfg, repo)

    # Patch global manager in backend.main
    main_mod.app_manager = mgr
    await mgr.initialize()

    worker = mgr.get_worker("A")
    if worker and worker.state:
        from backend.domain.types import DeviceStatus, SignalColor, StepKind, StepState, StepStatus
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

    transport = ASGITransport(app=main_mod.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    await mgr.shutdown()
    if os.path.exists(path):
        os.remove(path)


@pytest.mark.asyncio
async def test_health_and_junction_list(client: AsyncClient):
    res = await client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "HEALTHY"
    assert "A" in data["active_junctions"]

    res_list = await client.get("/api/junctions")
    assert res_list.status_code == 200
    j_list = res_list.json()
    assert len(j_list) == 1
    assert j_list[0]["junction_id"] == "A"


@pytest.mark.asyncio
async def test_junction_status(client: AsyncClient):
    res = await client.get("/api/junctions/A/status")
    assert res.status_code == 200
    status_data = res.json()
    assert status_data["junction_id"] == "A"
    assert "step" in status_data
    assert "desired_signals" in status_data
    assert "queues" in status_data
    assert "emergency" in status_data
    assert "manual" in status_data
    assert "failure" in status_data


@pytest.mark.asyncio
async def test_sensor_events_workflow(client: AsyncClient):
    now = datetime.now(timezone.utc).isoformat()

    # 1. Arrival of a truck
    res = await client.post(
        "/api/sensor-events",
        json={
            "event_id": "evt-api-101",
            "junction_id": "A",
            "direction": "NORTH",
            "event_type": "VEHICLE_ARRIVED",
            "vehicle_id": "TRUCK-101",
            "vehicle_type": "TRUCK",
            "sequence_no": 1,
            "timestamp": now,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["outcome"] == "ACCEPTED"
    assert data["queues"]["NORTH"] == 1

    # 2. Duplicate submission returns 200 with DUPLICATE_IGNORED (Idempotency!)
    res_dup = await client.post(
        "/api/sensor-events",
        json={
            "event_id": "evt-api-101",
            "junction_id": "A",
            "direction": "NORTH",
            "event_type": "VEHICLE_ARRIVED",
            "vehicle_id": "TRUCK-101",
            "vehicle_type": "TRUCK",
            "sequence_no": 1,
            "timestamp": now,
        },
    )
    assert res_dup.status_code == 200
    assert res_dup.json()["outcome"] == "DUPLICATE_IGNORED"
    assert res_dup.json()["queues"]["NORTH"] == 1

    # 3. Clearance
    res_clr = await client.post(
        "/api/sensor-events",
        json={
            "event_id": "evt-api-102",
            "junction_id": "A",
            "direction": "NORTH",
            "event_type": "VEHICLE_CLEARED",
            "vehicle_id": "TRUCK-101",
            "sequence_no": 2,
            "timestamp": now,
        },
    )
    assert res_clr.status_code == 200
    assert res_clr.json()["outcome"] == "ACCEPTED"
    assert res_clr.json()["queues"]["NORTH"] == 0


@pytest.mark.asyncio
async def test_manual_override_and_return_to_automatic(client: AsyncClient):
    # Request manual green for WEST
    res = await client.post(
        "/api/junctions/A/commands",
        json={"command": "MANUAL_GREEN_REQUEST", "direction": "WEST", "requested_by": "evaluator"},
    )
    assert res.status_code == 202
    assert res.json()["mode"] == "MANUAL"

    # Return to automatic
    res_ret = await client.post(
        "/api/junctions/A/commands",
        json={"command": "RETURN_TO_AUTOMATIC", "requested_by": "evaluator"},
    )
    assert res_ret.status_code == 202


@pytest.mark.asyncio
async def test_simulator_controller_behavior(client: AsyncClient):
    res = await client.post(
        "/api/simulator/controller-behavior",
        json={"junction_id": "A", "behavior": "DROP"},
    )
    assert res.status_code == 200
    assert res.json()["behavior"] == "DROP"


@pytest.mark.asyncio
async def test_audit_history_query(client: AsyncClient):
    res = await client.get("/api/junctions/A/history?limit=10")
    assert res.status_code == 200
    hist = res.json()
    assert "items" in hist
    assert len(hist["items"]) > 0
