from datetime import datetime
from typing import Dict, List, Optional, Tuple
from backend.config.config import DefaultsConfig
from backend.domain.types import (
    AuditRecord,
    AuditSeverity,
    DeviceStatus,
    JunctionState,
    VehicleRecord,
    VehicleStatus,
    VehicleType,
)


class EmergencyHandler:
    """Handles emergency preemption, conflicting emergency arbitration, and stale timeouts."""

    @classmethod
    def get_waiting_emergencies(cls, state: JunctionState) -> List[VehicleRecord]:
        return [
            v
            for v in state.vehicles.values()
            if v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY
        ]

    @classmethod
    def find_phase_for_direction(cls, state: JunctionState, direction: str) -> Optional[str]:
        for p_name, dirs in state.phases.items():
            if direction in dirs:
                return p_name
        return None

    @classmethod
    def determine_emergency_target(
        cls,
        state: JunctionState,
        now: datetime,
        config: DefaultsConfig,
    ) -> Optional[str]:
        """Determines the target phase for emergency service.

        Handles single and competing emergencies with oldest-first arbitration and hold cap alternation.
        """
        emergencies = cls.get_waiting_emergencies(state)
        if not emergencies:
            return None

        # Group by phase
        phase_emergencies: Dict[str, List[VehicleRecord]] = {}
        for ev in emergencies:
            p = cls.find_phase_for_direction(state, ev.direction)
            if p:
                phase_emergencies.setdefault(p, []).append(ev)

        if not phase_emergencies:
            return None

        # Single phase with emergencies
        if len(phase_emergencies) == 1:
            return next(iter(phase_emergencies.keys()))

        # Multiple phases with competing emergencies:
        current_serving = state.emergency.serving_phase
        if current_serving and current_serving in phase_emergencies:
            # Check hold cap
            if state.emergency.hold_started_at:
                hold_duration = (now - state.emergency.hold_started_at).total_seconds()
                if hold_duration >= config.emergency.hold_cap_seconds:
                    # Alternation: pick the other phase with oldest waiting emergency
                    other_phases = [p for p in phase_emergencies.keys() if p != current_serving]
                    oldest_other = min(
                        other_phases,
                        key=lambda p: min(v.arrived_at_server for v in phase_emergencies[p]),
                    )
                    return oldest_other
            return current_serving

        # No active serving phase or current serving phase cleared: oldest waiting emergency wins
        oldest_phase = min(
            phase_emergencies.keys(),
            key=lambda p: min(v.arrived_at_server for v in phase_emergencies[p]),
        )
        return oldest_phase

    @classmethod
    def clean_stale_emergencies(
        cls,
        state: JunctionState,
        now: datetime,
        config: DefaultsConfig,
    ) -> Tuple[List[str], List[AuditRecord]]:
        """Expires stale emergency vehicles that never received a CLEARED event."""
        expired_ids: List[str] = []
        audit_records: List[AuditRecord] = []

        for v in list(state.vehicles.values()):
            if v.status == VehicleStatus.WAITING and v.vehicle_type == VehicleType.EMERGENCY:
                # During FAILURE, lights cannot turn green so emergency cannot clear; do not expire
                if state.failure.active:
                    continue
                waited = (now - v.arrived_at_server).total_seconds()
                if waited >= config.emergency.stale_timeout_seconds:
                    v.status = VehicleStatus.CLEARED
                    expired_ids.append(v.vehicle_id)

                    # Flag direction sensor as suspect/warning
                    if v.direction in state.directions:
                        state.directions[v.direction].sensor_status = DeviceStatus.WARNING

                    audit_records.append(
                        AuditRecord(
                            junction_id=state.junction_id,
                            timestamp=now,
                            event_type="EMERGENCY_TIMEOUT",
                            severity=AuditSeverity.WARNING,
                            direction=v.direction,
                            vehicle_id=v.vehicle_id,
                            reason=f"Emergency vehicle {v.vehicle_id} exceeded stale timeout ({waited:.1f}s) without clearance; treated as cleared",
                        )
                    )

        return expired_ids, audit_records
