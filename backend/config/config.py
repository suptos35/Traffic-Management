from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field, model_validator


class TimingConfig(BaseModel):
    green_max_seconds: float = 30.0
    green_min_seconds: float = 10.0
    green_min_emergency_seconds: float = 3.0
    degraded_green_seconds: float = 20.0
    yellow_seconds: float = 5.0
    all_red_seconds: float = 2.0

    @model_validator(mode="after")
    def validate_timings(self) -> "TimingConfig":
        if self.yellow_seconds <= 0:
            raise ValueError("yellow_seconds must be > 0")
        if self.all_red_seconds <= 0:
            raise ValueError("all_red_seconds must be > 0")
        if self.degraded_green_seconds <= 0:
            raise ValueError("degraded_green_seconds must be > 0")
        if not (self.green_min_emergency_seconds <= self.green_min_seconds <= self.green_max_seconds):
            raise ValueError("Must satisfy green_min_emergency_seconds <= green_min_seconds <= green_max_seconds")
        return self


class SchedulingConfig(BaseModel):
    aging_weight_per_second: float = 0.05
    max_wait_seconds: float = 120.0
    switch_margin_ratio: float = 1.25

    @model_validator(mode="after")
    def validate_scheduling(self) -> "SchedulingConfig":
        if self.aging_weight_per_second < 0:
            raise ValueError("aging_weight_per_second must be >= 0")
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be > 0")
        if self.switch_margin_ratio < 1.0:
            raise ValueError("switch_margin_ratio must be >= 1.0")
        return self


class EmergencyConfig(BaseModel):
    hold_cap_seconds: float = 45.0
    stale_timeout_seconds: float = 120.0
    long_hold_warning_seconds: float = 120.0

    @model_validator(mode="after")
    def validate_emergency(self) -> "EmergencyConfig":
        if self.hold_cap_seconds <= 0:
            raise ValueError("hold_cap_seconds must be > 0")
        if self.stale_timeout_seconds <= 0:
            raise ValueError("stale_timeout_seconds must be > 0")
        return self


class ManualConfig(BaseModel):
    lease_seconds: float = 120.0

    @model_validator(mode="after")
    def validate_manual(self) -> "ManualConfig":
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be > 0")
        return self


class EventsConfig(BaseModel):
    enforce_timestamp_window: bool = True
    max_event_age_seconds: float = 120.0
    max_future_skew_seconds: float = 5.0
    vehicle_ghost_expiry_seconds: float = 600.0
    remember_seconds: float = 86400.0


class ControllerConfig(BaseModel):
    ack_timeout_seconds: float = 2.0
    max_retries: int = 2
    state_refresh_seconds: float = 30.0
    failure_resend_seconds: float = 10.0
    recovery_stable_seconds: float = 10.0

    @model_validator(mode="after")
    def validate_controller(self) -> "ControllerConfig":
        if self.ack_timeout_seconds <= 0:
            raise ValueError("ack_timeout_seconds must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        min_refresh = self.ack_timeout_seconds * (self.max_retries + 1)
        if self.state_refresh_seconds <= min_refresh:
            raise ValueError(
                f"state_refresh_seconds ({self.state_refresh_seconds}) must be > "
                f"ack_timeout_seconds * (max_retries + 1) ({min_refresh})"
            )
        if self.failure_resend_seconds < self.ack_timeout_seconds:
            raise ValueError("failure_resend_seconds must be >= ack_timeout_seconds")
        if self.recovery_stable_seconds < 0:
            raise ValueError("recovery_stable_seconds must be >= 0")
        return self


class DefaultsConfig(BaseModel):
    timing: TimingConfig = Field(default_factory=TimingConfig)
    tick_interval_ms: int = 500
    vehicle_priority_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "TRUCK": 3.0,
            "FORKLIFT": 2.0,
            "EMPLOYEE_VEHICLE": 1.0,
            "UNKNOWN": 0.5,
        }
    )
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
    emergency: EmergencyConfig = Field(default_factory=EmergencyConfig)
    manual: ManualConfig = Field(default_factory=ManualConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    controller: ControllerConfig = Field(default_factory=ControllerConfig)

    @model_validator(mode="after")
    def validate_weights(self) -> "DefaultsConfig":
        for k, v in self.vehicle_priority_weights.items():
            if v <= 0:
                raise ValueError(f"Priority weight for {k} must be > 0")
        return self


class PhaseDefinition(BaseModel):
    name: str
    directions: List[str]


class JunctionDefinition(BaseModel):
    directions: List[str]
    phases: List[PhaseDefinition]
    overrides: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_phases_and_directions(self) -> "JunctionDefinition":
        dir_set = set(self.directions)
        if len(dir_set) != len(self.directions):
            raise ValueError("Directions list contains duplicates")

        assigned_dirs = []
        for phase in self.phases:
            for d in phase.directions:
                if d not in dir_set:
                    raise ValueError(f"Direction {d} in phase {phase.name} not in junction directions")
                assigned_dirs.append(d)

        # Every direction must belong to exactly one phase
        if sorted(assigned_dirs) != sorted(self.directions):
            raise ValueError(
                f"Every direction must belong to exactly one phase. Assigned: {assigned_dirs}, Expected: {self.directions}"
            )
        return self


class AuditConfig(BaseModel):
    retention_days: int = 30


class PersistenceConfig(BaseModel):
    db_path: str = "traffic.db"
    housekeeping_interval_seconds: float = 3600.0
    max_consecutive_commit_failures: int = 3


class ApiConfig(BaseModel):
    inbox_max_size: int = 1000
    request_timeout_ms: int = 2000
    history_default_limit: int = 50
    history_max_limit: int = 200
    host: str = "0.0.0.0"
    port: int = 8000


class SimulatorConfig(BaseModel):
    enabled: bool = True
    behavior: str = "AUTO_ACK"
    auto_ack_delay_ms: int = 300


class FrontendConfig(BaseModel):
    poll_interval_ms: int = 1000
    history_poll_interval_ms: int = 2000
    stale_data_seconds: float = 5.0


class AppConfig(BaseModel):
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    junctions: Dict[str, JunctionDefinition] = Field(default_factory=dict)

    @classmethod
    def load_from_yaml(cls, path: str | Path) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)


def get_junction_effective_config(app_config: AppConfig, junction_id: str) -> DefaultsConfig:
    """Returns effective DefaultsConfig for a junction with any overrides applied."""
    junction = app_config.junctions.get(junction_id)
    if not junction or not junction.overrides:
        return app_config.defaults.model_copy(deep=True)

    base_dict = app_config.defaults.model_dump()

    # Deep merge overrides
    def deep_merge(target: dict, source: dict):
        for k, v in source.items():
            if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                deep_merge(target[k], v)
            else:
                target[k] = v

    deep_merge(base_dict, junction.overrides)
    return DefaultsConfig(**base_dict)
