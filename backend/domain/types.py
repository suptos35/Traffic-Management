from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class SignalColor(str, Enum):
    RED = "RED"
    YELLOW = "YELLOW"
    GREEN = "GREEN"
    UNKNOWN = "UNKNOWN"


class StepKind(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ALL_RED = "ALL_RED"
    RECOVERY_YELLOW = "RECOVERY_YELLOW"
    RECOVERY_RED = "RECOVERY_RED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"


class Mode(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"
    DEGRADED = "DEGRADED"
    FAILURE = "FAILURE"


class DeviceType(str, Enum):
    SIGNAL_CONTROLLER = "SIGNAL_CONTROLLER"
    SENSOR = "SENSOR"


class DeviceStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    WARNING = "WARNING"
    UNKNOWN = "UNKNOWN"


class VehicleType(str, Enum):
    EMERGENCY = "EMERGENCY"
    TRUCK = "TRUCK"
    FORKLIFT = "FORKLIFT"
    EMPLOYEE_VEHICLE = "EMPLOYEE_VEHICLE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, s: Optional[str]) -> "VehicleType":
        if not s:
            return cls.UNKNOWN
        try:
            return cls(s.upper())
        except ValueError:
            return cls.UNKNOWN


class VehicleStatus(str, Enum):
    WAITING = "WAITING"
    CLEARED = "CLEARED"


class EventOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_NO_CHANGE = "ACCEPTED_NO_CHANGE"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    IGNORED_OUT_OF_ORDER = "IGNORED_OUT_OF_ORDER"
    IGNORED = "IGNORED"
    REJECTED_INVALID = "REJECTED_INVALID"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_UNKNOWN_JUNCTION = "REJECTED_UNKNOWN_JUNCTION"


class CommandStatus(str, Enum):
    PENDING = "PENDING"
    ACKED = "ACKED"
    NACKED = "NACKED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    ABANDONED = "ABANDONED"


class AuditSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AuditEventType(str, Enum):
    SIGNAL_CHANGED = "SIGNAL_CHANGED"
    SIGNAL_TRANSITION_STARTED = "SIGNAL_TRANSITION_STARTED"
    SIGNAL_STATE_REQUESTED = "SIGNAL_STATE_REQUESTED"
    VEHICLE_DETECTED = "VEHICLE_DETECTED"
    VEHICLE_CLEARED = "VEHICLE_CLEARED"
    SENSOR_EVENT_DUPLICATE = "SENSOR_EVENT_DUPLICATE"
    SENSOR_EVENT_REJECTED = "SENSOR_EVENT_REJECTED"
    SENSOR_EVENT_IGNORED = "SENSOR_EVENT_IGNORED"
    EMERGENCY_DETECTED = "EMERGENCY_DETECTED"
    EMERGENCY_CLEARED = "EMERGENCY_CLEARED"
    EMERGENCY_TIMEOUT = "EMERGENCY_TIMEOUT"
    MANUAL_OVERRIDE_STARTED = "MANUAL_OVERRIDE_STARTED"
    MANUAL_OVERRIDE_RENEWED = "MANUAL_OVERRIDE_RENEWED"
    MANUAL_OVERRIDE_ENDED = "MANUAL_OVERRIDE_ENDED"
    MANUAL_OVERRIDE_SUSPENDED = "MANUAL_OVERRIDE_SUSPENDED"
    MANUAL_OVERRIDE_RESUMED = "MANUAL_OVERRIDE_RESUMED"
    RETURN_TO_AUTOMATIC = "RETURN_TO_AUTOMATIC"
    ADMIN_COMMAND_REJECTED = "ADMIN_COMMAND_REJECTED"
    CONTROLLER_ACK = "CONTROLLER_ACK"
    ACK_LATE = "ACK_LATE"
    ACK_DUPLICATE_IGNORED = "ACK_DUPLICATE_IGNORED"
    ACK_IGNORED = "ACK_IGNORED"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    COMMAND_RETRY = "COMMAND_RETRY"
    COMMAND_FAILED = "COMMAND_FAILED"
    DEVICE_STATUS_CHANGED = "DEVICE_STATUS_CHANGED"
    DEVICE_STATUS_INFERRED_ONLINE = "DEVICE_STATUS_INFERRED_ONLINE"
    FAILURE_ENTERED = "FAILURE_ENTERED"
    FAILURE_EXITED = "FAILURE_EXITED"
    DEGRADED_ENTERED = "DEGRADED_ENTERED"
    DEGRADED_EXITED = "DEGRADED_EXITED"
    MODE_CHANGED = "MODE_CHANGED"
    STATE_MISMATCH = "STATE_MISMATCH"
    DEVICE_EVENT_DUPLICATE = "DEVICE_EVENT_DUPLICATE"
    DEVICE_EVENT_OUT_OF_ORDER = "DEVICE_EVENT_OUT_OF_ORDER"
    VEHICLE_EXPIRED = "VEHICLE_EXPIRED"
    SENSOR_QUEUE_FLUSHED = "SENSOR_QUEUE_FLUSHED"
    WARNING_RAISED = "WARNING_RAISED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    RESTART_RECOVERY = "RESTART_RECOVERY"
    JUNCTION_CREATED = "JUNCTION_CREATED"
    RECOVERY_STABLE = "RECOVERY_STABLE"


@dataclass
class ControllerCommand:
    command_id: str
    junction_id: str
    direction: str
    requested_state: SignalColor
    attempt: int
    sent_at: datetime
    status: CommandStatus = CommandStatus.PENDING


@dataclass
class AuditRecord:
    junction_id: str
    timestamp: datetime
    event_type: str
    severity: AuditSeverity
    reason: str
    direction: Optional[str] = None
    previous_state: Optional[SignalColor] = None
    new_state: Optional[SignalColor] = None
    command_id: Optional[str] = None
    cause_event_id: Optional[str] = None
    transition_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    mode: Optional[Mode] = None
    details: Dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None


@dataclass
class StepState:
    kind: StepKind
    phase: Optional[str]
    status: StepStatus
    started_at: datetime
    confirmed_at: Optional[datetime] = None


@dataclass
class DirectionState:
    desired: SignalColor
    actual: SignalColor
    pending_command: Optional[ControllerCommand] = None
    last_confirmed_at: Optional[datetime] = None
    signal_controller_status: DeviceStatus = DeviceStatus.UNKNOWN
    sensor_status: DeviceStatus = DeviceStatus.ONLINE
    last_status_timestamp: Optional[datetime] = None


@dataclass
class VehicleRecord:
    vehicle_id: str
    vehicle_type: VehicleType
    direction: str
    status: VehicleStatus
    arrived_at_server: datetime
    arrived_at_sensor: datetime
    last_sequence_no: int


@dataclass
class ProcessedEventRecord:
    event_id: str
    received_at: datetime
    outcome: EventOutcome
    payload_hash: str = ""


@dataclass
class ManualState:
    active: bool = False
    target_phase: Optional[str] = None
    expires_at: Optional[datetime] = None
    requested_by: Optional[str] = None
    suspended: bool = False


@dataclass
class EmergencyState:
    active: bool = False
    serving_phase: Optional[str] = None
    hold_started_at: Optional[datetime] = None


@dataclass
class FailureState:
    active: bool = False
    since: Optional[datetime] = None
    reason: Optional[str] = None
    recovery_stable_since: Optional[datetime] = None


@dataclass
class Alert:
    code: str
    severity: AuditSeverity
    since: datetime
    message: str
    direction: Optional[str] = None


@dataclass
class JunctionState:
    junction_id: str
    step: StepState
    next_target: Optional[str]
    directions: Dict[str, DirectionState]
    phases: Dict[str, List[str]]
    vehicles: Dict[str, VehicleRecord] = field(default_factory=dict)
    processed_events: Dict[str, ProcessedEventRecord] = field(default_factory=dict)
    manual: ManualState = field(default_factory=ManualState)
    emergency: EmergencyState = field(default_factory=EmergencyState)
    failure: FailureState = field(default_factory=FailureState)
    command_counter: int = 0
    transition_counter: int = 0
    updated_at: Optional[datetime] = None


# Inputs
@dataclass
class SensorEventInput:
    event_id: str
    junction_id: str
    direction: str
    event_type: str  # VEHICLE_ARRIVED or VEHICLE_CLEARED
    vehicle_id: str
    sequence_no: int
    timestamp: datetime
    vehicle_type: Optional[str] = None
    raw_payload: Optional[str] = None


@dataclass
class AdminCommandInput:
    command: str  # MANUAL_GREEN_REQUEST or RETURN_TO_AUTOMATIC
    direction: Optional[str] = None
    requested_by: Optional[str] = None


@dataclass
class TickInput:
    pass


@dataclass
class ControllerReplyInput:
    command_id: str
    status: str  # ACK or NACK
    actual_state: Optional[SignalColor] = None
    junction_id: Optional[str] = None
    timestamp: Optional[datetime] = None


@dataclass
class DeviceStatusInput:
    event_id: str
    junction_id: str
    device_type: DeviceType
    status: DeviceStatus
    timestamp: datetime
    direction: Optional[str] = None


@dataclass
class StartupInput:
    pass


EngineInput = Union[
    SensorEventInput,
    AdminCommandInput,
    TickInput,
    ControllerReplyInput,
    DeviceStatusInput,
    StartupInput,
]


# Actions
@dataclass
class SendSignalCommandAction:
    command: ControllerCommand


@dataclass
class AuditAction:
    record: AuditRecord


EngineAction = Union[SendSignalCommandAction, AuditAction]
