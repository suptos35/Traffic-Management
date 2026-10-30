from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------
class SensorEventRequest(BaseModel):
    event_id: str
    junction_id: str
    direction: str
    event_type: str  # VEHICLE_ARRIVED or VEHICLE_CLEARED
    vehicle_id: str
    sequence_no: int = Field(ge=0)
    timestamp: datetime
    vehicle_type: Optional[str] = None


class AdminCommandRequest(BaseModel):
    command: str  # MANUAL_GREEN_REQUEST or RETURN_TO_AUTOMATIC
    direction: Optional[str] = None
    requested_by: Optional[str] = "admin"


class ControllerEventRequest(BaseModel):
    command_id: str
    status: str  # ACK or NACK
    actual_state: Optional[str] = None
    junction_id: Optional[str] = None
    timestamp: Optional[datetime] = None


class DeviceEventRequest(BaseModel):
    event_id: str
    junction_id: str
    device_type: str  # SIGNAL_CONTROLLER or SENSOR
    status: str  # ONLINE, OFFLINE, DEGRADED, WARNING, UNKNOWN
    timestamp: datetime
    direction: Optional[str] = None


class ControllerBehaviorRequest(BaseModel):
    junction_id: str
    behavior: str  # AUTO_ACK, HOLD, DROP, WRONG_STATE, NACK
    auto_ack_delay_ms: Optional[int] = None


class CreateJunctionRequest(BaseModel):
    junction_id: str
    directions: List[str]
    phases: List[Dict[str, Any]]
    overrides: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------
# Response Schemas
# ---------------------------------------------------------
class SensorEventResponse(BaseModel):
    outcome: str
    event_id: str
    junction_id: str
    queues: Dict[str, int]
    message: str


class AdminCommandResponse(BaseModel):
    outcome: str
    mode: str
    manual_expires_at: Optional[datetime] = None
    message: str


class StepDetail(BaseModel):
    kind: str
    phase: Optional[str] = None
    status: str
    started_at: datetime
    confirmed_at: Optional[datetime] = None
    phase_ends_at: Optional[datetime] = None
    next_target: Optional[str] = None


class EmergencyDetail(BaseModel):
    active: bool
    vehicles: List[Dict[str, Any]] = []
    serving_phase: Optional[str] = None
    hold_started_at: Optional[datetime] = None


class ManualDetail(BaseModel):
    active: bool
    suspended: bool
    target_phase: Optional[str] = None
    expires_at: Optional[datetime] = None
    seconds_remaining: Optional[float] = None
    requested_by: Optional[str] = None


class FailureDetail(BaseModel):
    active: bool
    since: Optional[datetime] = None
    reason: Optional[str] = None
    recovery_stable_since: Optional[datetime] = None


class AlertDetail(BaseModel):
    code: str
    severity: str
    direction: Optional[str] = None
    since: datetime
    message: str


class JunctionStatusResponse(BaseModel):
    junction_id: str
    server_time: datetime
    mode: str
    phase: Optional[str] = None
    step: StepDetail
    controller_status: str
    sensor_status: str
    desired_signals: Dict[str, str]
    actual_signals: Dict[str, str]
    queues: Dict[str, int]
    emergency: EmergencyDetail
    manual: ManualDetail
    failure: FailureDetail
    devices: Dict[str, Dict[str, str]]
    pending_commands: List[Dict[str, Any]]
    alerts: List[AlertDetail]
    waiting_vehicles: List[Dict[str, Any]] = []


class JunctionSummaryItem(BaseModel):
    junction_id: str
    mode: str
    phase: Optional[str] = None
    controller_status: str
    sensor_status: str
    desired_signals: Dict[str, str]
    actual_signals: Dict[str, str]
    queues: Dict[str, int]
    emergency_active: bool
    failure_active: bool
    alerts_count: int


class HistoryResponse(BaseModel):
    items: List[Dict[str, Any]]
    next_cursor: Optional[int] = None
