from datetime import datetime
from typing import Dict, List, Optional
from backend.config.config import DefaultsConfig
from backend.domain.types import JunctionState, VehicleStatus, VehicleType


class TrafficScheduler:
    """Automatic traffic control scheduler with priority weighting, starvation protection, and aging."""

    @staticmethod
    def calculate_vehicle_score(
        vehicle_type: VehicleType,
        arrived_at_server: datetime,
        now: datetime,
        config: DefaultsConfig,
    ) -> float:
        if vehicle_type == VehicleType.EMERGENCY:
            return 0.0  # Emergencies are handled separately via preemption

        type_weight = config.vehicle_priority_weights.get(vehicle_type.value, 0.5)
        waited_seconds = max(0.0, (now - arrived_at_server).total_seconds())
        aging_score = config.scheduling.aging_weight_per_second * waited_seconds
        return type_weight + aging_score

    @classmethod
    def calculate_phase_scores(
        cls,
        state: JunctionState,
        now: datetime,
        config: DefaultsConfig,
    ) -> Dict[str, float]:
        scores: Dict[str, float] = {p_name: 0.0 for p_name in state.phases}

        for veh in state.vehicles.values():
            if veh.status != VehicleStatus.WAITING:
                continue
            if veh.vehicle_type == VehicleType.EMERGENCY:
                continue

            v_score = cls.calculate_vehicle_score(veh.vehicle_type, veh.arrived_at_server, now, config)

            # Find phase for vehicle's direction
            for p_name, dirs in state.phases.items():
                if veh.direction in dirs:
                    scores[p_name] += v_score
                    break

        return scores

    @classmethod
    def find_starved_phase(
        cls,
        state: JunctionState,
        current_phase: str,
        now: datetime,
        config: DefaultsConfig,
    ) -> Optional[str]:
        """Finds if any vehicle in OTHER phases has waited longer than max_wait_seconds."""
        starved_phase: Optional[str] = None
        max_waited = -1.0

        for veh in state.vehicles.values():
            if veh.status != VehicleStatus.WAITING:
                continue
            if veh.vehicle_type == VehicleType.EMERGENCY:
                continue

            waited = (now - veh.arrived_at_server).total_seconds()
            if waited >= config.scheduling.max_wait_seconds:
                # Find phase
                v_phase = None
                for p_name, dirs in state.phases.items():
                    if veh.direction in dirs:
                        v_phase = p_name
                        break

                if v_phase and v_phase != current_phase:
                    if waited > max_waited:
                        max_waited = waited
                        starved_phase = v_phase

        return starved_phase

    @classmethod
    def evaluate_automatic_switch(
        cls,
        state: JunctionState,
        now: datetime,
        config: DefaultsConfig,
    ) -> Optional[str]:
        """Evaluates whether to switch from the current GREEN phase to another phase.

        Returns target phase name if switch should start, or None to stay green.
        """
        current_phase = state.step.phase
        if not current_phase or state.step.confirmed_at is None:
            return None

        # Dwell time counted from confirmation
        green_dwell = (now - state.step.confirmed_at).total_seconds()

        # 1. Respect minimum green time
        if green_dwell < config.timing.green_min_seconds:
            return None

        # 2. Check for starved vehicles in other phases (hard cap)
        starved_phase = cls.find_starved_phase(state, current_phase, now, config)
        if starved_phase:
            return starved_phase

        # 3. Score-based selection
        scores = cls.calculate_phase_scores(state, now, config)
        current_score = scores.get(current_phase, 0.0)

        # Find best other phase
        other_phases = [p for p in state.phases if p != current_phase]
        if not other_phases:
            return None

        best_other_phase = max(other_phases, key=lambda p: scores.get(p, 0.0))
        best_other_score = scores.get(best_other_phase, 0.0)

        # If nobody waiting elsewhere, stay green
        if best_other_score == 0.0:
            return None

        # If nobody waiting in current phase, but someone waiting elsewhere, switch
        if current_score == 0.0:
            return best_other_phase

        # Max green reached: switch if someone waiting elsewhere
        if green_dwell >= config.timing.green_max_seconds:
            return best_other_phase

        # Challenger must clearly beat current phase by switch_margin_ratio
        if best_other_score > current_score * config.scheduling.switch_margin_ratio:
            return best_other_phase

        return None

    @classmethod
    def evaluate_degraded_switch(
        cls,
        state: JunctionState,
        now: datetime,
        config: DefaultsConfig,
    ) -> Optional[str]:
        """Fixed-time round-robin scheduler used during DEGRADED mode."""
        current_phase = state.step.phase
        if not current_phase or state.step.confirmed_at is None:
            return None

        green_dwell = (now - state.step.confirmed_at).total_seconds()
        if green_dwell >= config.timing.degraded_green_seconds:
            # Round-robin to next phase
            phase_list = list(state.phases.keys())
            curr_idx = phase_list.index(current_phase)
            next_idx = (curr_idx + 1) % len(phase_list)
            return phase_list[next_idx]

        return None
