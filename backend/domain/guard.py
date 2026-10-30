from typing import Dict, List, Tuple
from backend.domain.types import ControllerCommand, DirectionState, SignalColor


class SafetyGuard:
    """Layer 2 Safety Guard: Safe-list verification of signal commands and states."""

    @staticmethod
    def is_direction_confirmed_red(dir_state: DirectionState) -> bool:
        """A direction is confirmed RED only if actual is RED and no pending command asks for non-RED."""
        if dir_state.actual != SignalColor.RED:
            return False
        if dir_state.pending_command is not None:
            if dir_state.pending_command.requested_state != SignalColor.RED:
                return False
        return True

    @classmethod
    def validate_command(
        cls,
        cmd: ControllerCommand,
        target_phase: str | None,
        phases: Dict[str, List[str]],
        directions: Dict[str, DirectionState],
    ) -> Tuple[bool, str]:
        """Validates a single outgoing controller command against confirmed physical state and conflicting directions."""
        dir_name = cmd.direction
        if dir_name not in directions:
            return False, f"Unknown direction '{dir_name}'"

        current_actual = directions[dir_name].actual
        requested = cmd.requested_state

        # 1. State refresh / heartbeat: same color is always allowed
        if current_actual == requested:
            return True, "Allowed: State refresh"

        # 2. Check transition from confirmed state
        if current_actual == SignalColor.GREEN:
            if requested != SignalColor.YELLOW:
                return False, f"Unsafe transition on {dir_name}: GREEN can only transition to YELLOW, not {requested}"

        elif current_actual == SignalColor.YELLOW:
            if requested != SignalColor.RED:
                return False, f"Unsafe transition on {dir_name}: YELLOW can only transition to RED, not {requested}"

        elif current_actual == SignalColor.RED:
            if requested == SignalColor.GREEN:
                # Must ensure EVERY conflicting direction is CONFIRMED RED
                if target_phase is None or target_phase not in phases:
                    return False, f"Cannot command GREEN on {dir_name} without an active target phase"

                target_phase_dirs = phases[target_phase]
                if dir_name not in target_phase_dirs:
                    return False, f"Direction {dir_name} is not in target phase {target_phase}"

                for other_dir, other_state in directions.items():
                    if other_dir not in target_phase_dirs:
                        if not cls.is_direction_confirmed_red(other_state):
                            return False, (
                                f"Cannot set {dir_name} to GREEN: conflicting direction "
                                f"{other_dir} is not confirmed RED (actual={other_state.actual})"
                            )
            elif requested != SignalColor.YELLOW:
                # RED -> YELLOW is not permitted in our model (YELLOW means stop soon, no yellow-before-green)
                return False, f"Unsafe transition on {dir_name}: RED cannot transition to {requested}"

        elif current_actual == SignalColor.UNKNOWN:
            # Unknown actual state might be GREEN, so we only permit transitioning to YELLOW first
            if requested != SignalColor.YELLOW and requested != SignalColor.RED:
                return False, f"Unsafe transition on {dir_name}: UNKNOWN state can only transition to YELLOW or RED"

        return True, "Safe"

    @classmethod
    def validate_state_combination(
        cls,
        desired_signals: Dict[str, SignalColor],
        phases: Dict[str, List[str]],
    ) -> Tuple[bool, str]:
        """Validates that a full combination of desired signals represents a safe state."""
        # Check how many phases have GREEN signals
        green_phases = set()
        for phase_name, p_dirs in phases.items():
            for d in p_dirs:
                if desired_signals.get(d) == SignalColor.GREEN:
                    green_phases.add(phase_name)

        if len(green_phases) > 1:
            return False, f"Conflicting greens detected across phases: {list(green_phases)}"

        # If a phase is green, all directions outside that phase MUST be RED
        if len(green_phases) == 1:
            active_phase = next(iter(green_phases))
            active_dirs = set(phases[active_phase])
            for d, col in desired_signals.items():
                if d not in active_dirs and col != SignalColor.RED:
                    return False, f"Direction {d} outside active green phase {active_phase} is {col}, expected RED"

        # If no green, any combination of RED and YELLOW is safe (all-red, yellow clearance, recovery)
        for d, col in desired_signals.items():
            if col not in (SignalColor.RED, SignalColor.YELLOW, SignalColor.GREEN):
                return False, f"Direction {d} has invalid signal color {col}"

        return True, "Safe combination"
