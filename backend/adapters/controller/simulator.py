import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional
from backend.config.config import SimulatorConfig
from backend.domain.types import ControllerCommand, ControllerReplyInput, SignalColor

logger = logging.getLogger(__name__)


class ControllerPort:
    """Interface for physical or simulated controller communication."""

    async def send_command(self, cmd: ControllerCommand) -> None:
        raise NotImplementedError


class RESTSimulatorController(ControllerPort):
    """Simulator controller port implementing configurable behavior:

    AUTO_ACK, HOLD, DROP, WRONG_STATE, NACK.
    """

    def __init__(self, config: SimulatorConfig, dispatch_reply_cb):
        self.config = config
        self.dispatch_reply_cb = dispatch_reply_cb  # async callback to enqueue ControllerReplyInput
        self.behavior: str = config.behavior
        self.auto_ack_delay_ms: int = config.auto_ack_delay_ms

    def set_behavior(self, behavior: str) -> None:
        self.behavior = behavior.upper()
        logger.info(f"Simulator controller behavior set to: {self.behavior}")

    async def send_command(self, cmd: ControllerCommand) -> None:
        if not self.config.enabled:
            logger.debug(f"Simulator disabled; dropping command {cmd.command_id}")
            return

        if self.behavior == "DROP":
            logger.info(f"[SIMULATOR DROP] Command {cmd.command_id} on {cmd.direction} dropped silently")
            return

        if self.behavior == "HOLD":
            logger.info(f"[SIMULATOR HOLD] Command {cmd.command_id} held; waiting for manual ACK/NACK")
            return

        # Handle async simulated response
        delay_seconds = self.auto_ack_delay_ms / 1000.0

        async def _delayed_reply():
            await asyncio.sleep(delay_seconds)
            now = datetime.now(timezone.utc)

            if self.behavior == "AUTO_ACK":
                reply = ControllerReplyInput(
                    command_id=cmd.command_id,
                    status="ACK",
                    actual_state=cmd.requested_state,
                    junction_id=cmd.junction_id,
                    timestamp=now,
                )
            elif self.behavior == "WRONG_STATE":
                wrong_state = SignalColor.RED if cmd.requested_state != SignalColor.RED else SignalColor.GREEN
                reply = ControllerReplyInput(
                    command_id=cmd.command_id,
                    status="ACK",
                    actual_state=wrong_state,
                    junction_id=cmd.junction_id,
                    timestamp=now,
                )
            elif self.behavior == "NACK":
                reply = ControllerReplyInput(
                    command_id=cmd.command_id,
                    status="NACK",
                    actual_state=None,
                    junction_id=cmd.junction_id,
                    timestamp=now,
                )
            else:
                logger.warning(f"Unknown simulator behavior '{self.behavior}', defaulting to AUTO_ACK")
                reply = ControllerReplyInput(
                    command_id=cmd.command_id,
                    status="ACK",
                    actual_state=cmd.requested_state,
                    junction_id=cmd.junction_id,
                    timestamp=now,
                )

            await self.dispatch_reply_cb(reply)

        asyncio.create_task(_delayed_reply())
