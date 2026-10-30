import asyncio
from datetime import datetime, timezone
import logging
from typing import List, Optional, Tuple
from backend.adapters.controller.simulator import ControllerPort, RESTSimulatorController
from backend.adapters.persistence.repository import SQLiteRepository
from backend.config.config import AppConfig, DefaultsConfig, get_junction_effective_config
from backend.domain.engine import TrafficEngine, compute_mode, create_initial_junction_state
from backend.domain.types import (
    AuditRecord,
    AuditSeverity,
    ControllerCommand,
    EngineAction,
    EngineInput,
    EventOutcome,
    JunctionState,
    Mode,
    SendSignalCommandAction,
    StartupInput,
    TickInput,
)

logger = logging.getLogger(__name__)


class InboxFullError(Exception):
    pass


class JunctionWorker:
    """Manages the serialized inbox and worker loop for one traffic junction."""

    def __init__(
        self,
        junction_id: str,
        app_config: AppConfig,
        repository: SQLiteRepository,
        initial_state: Optional[JunctionState] = None,
    ):
        self.junction_id = junction_id
        self.app_config = app_config
        self.config: DefaultsConfig = get_junction_effective_config(app_config, junction_id)
        self.repository = repository

        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=self.app_config.api.inbox_max_size)
        self.state: Optional[JunctionState] = initial_state
        self.worker_task: Optional[asyncio.Task] = None
        self.ticker_task: Optional[asyncio.Task] = None
        self.housekeeping_task: Optional[asyncio.Task] = None
        self._running = False
        self.consecutive_commit_failures = 0

        # Simulator Controller Port
        self.controller: RESTSimulatorController = RESTSimulatorController(
            config=self.app_config.simulator,
            dispatch_reply_cb=self.submit_internal,
        )

    async def start(self) -> None:
        """Starts worker loop, loads or creates state, runs startup recovery, and launches ticker."""
        if self._running:
            return
        self._running = True

        # Load from DB or initialize from config
        now = datetime.now(timezone.utc)
        loaded = self.repository.load_junction_state(self.junction_id)
        if loaded:
            self.state = loaded
            logger.info(f"Loaded existing junction state for '{self.junction_id}' from database")
        else:
            j_def = self.app_config.junctions.get(self.junction_id)
            if not j_def:
                raise ValueError(f"No definition found for junction '{self.junction_id}'")
            phases = {p.name: p.directions for p in j_def.phases}
            self.state = create_initial_junction_state(self.junction_id, j_def.directions, phases, now)
            # Initial persist
            self.repository.save_junction_transaction(self.state, [], [])
            logger.info(f"Created new junction state for '{self.junction_id}'")

        # Start worker task
        self.worker_task = asyncio.create_task(self._worker_loop())

        # Submit startup recovery
        await self.submit(StartupInput())

        # Start ticker task
        self.ticker_task = asyncio.create_task(self._ticker_loop())

        # Start housekeeping task
        self.housekeeping_task = asyncio.create_task(self._housekeeping_loop())

    async def stop(self) -> None:
        """Stops worker and ticker loops gracefully."""
        self._running = False
        if self.ticker_task:
            self.ticker_task.cancel()
        if self.housekeeping_task:
            self.housekeeping_task.cancel()
        if self.worker_task:
            self.worker_task.cancel()

    async def submit_internal(self, event_input: EngineInput) -> None:
        """Submits an internal event (e.g. controller reply or tick) without waiting for a reply."""
        try:
            now = datetime.now(timezone.utc)
            self.inbox.put_nowait((event_input, None, now))
        except asyncio.QueueFull:
            logger.error(f"Inbox full for junction {self.junction_id}; dropped internal event {event_input}")

    async def submit(self, event_input: EngineInput, timeout_ms: Optional[int] = None) -> Tuple[EventOutcome, str]:
        """Submits an external input, waiting for the inbox worker to process and commit it."""
        if not self._running:
            raise RuntimeError("JunctionWorker is not running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        now = datetime.now(timezone.utc)

        try:
            self.inbox.put_nowait((event_input, future, now))
        except asyncio.QueueFull:
            raise InboxFullError("Junction inbox is full")

        timeout_sec = (timeout_ms or self.app_config.api.request_timeout_ms) / 1000.0
        try:
            outcome, msg = await asyncio.wait_for(future, timeout=timeout_sec)
            return outcome, msg
        except asyncio.TimeoutError:
            raise TimeoutError("Request timed out waiting for junction inbox processing")

    async def _worker_loop(self) -> None:
        """Sequential single-threaded event loop for this junction."""
        while self._running:
            try:
                event_input, future, received_time = await self.inbox.get()
            except asyncio.CancelledError:
                break

            try:
                outcome, msg = await self._process_single_input(event_input, received_time)
                if future and not future.done():
                    future.set_result((outcome, msg))
            except Exception as e:
                logger.exception(f"Error processing input for junction {self.junction_id}: {e}")
                if future and not future.done():
                    future.set_exception(e)
            finally:
                self.inbox.task_done()

    async def _process_single_input(
        self,
        event_input: EngineInput,
        received_time: datetime,
    ) -> Tuple[EventOutcome, str]:
        assert self.state is not None

        # 1. Pure domain decision
        self.state, actions, outcome, msg = TrafficEngine.decide(
            self.state, event_input, self.config, received_time
        )

        # 2. Extract audit records and commands
        audit_records: List[AuditRecord] = []
        commands_to_send: List[ControllerCommand] = []

        for act in actions:
            if isinstance(act, AuditRecord):
                audit_records.append(act)
            elif isinstance(act, SendSignalCommandAction):
                commands_to_send.append(act.command)

        # 3. Transactional persistence (Commit before send)
        try:
            self.repository.save_junction_transaction(self.state, audit_records, commands_to_send)
            self.consecutive_commit_failures = 0
        except Exception as db_err:
            self.consecutive_commit_failures += 1
            logger.error(f"Database commit failed ({self.consecutive_commit_failures} in a row): {db_err}")
            if self.consecutive_commit_failures >= self.app_config.persistence.max_consecutive_commit_failures:
                # Force junction into failure mode if database repeatedly fails
                logger.critical(f"Max consecutive DB commit failures reached for {self.junction_id}; entering FAILURE")
                self.state.failure.active = True
                self.state.failure.since = received_time
                self.state.failure.reason = f"Database commit failed {self.consecutive_commit_failures} times"
            raise db_err

        # 4. Hand commands to Controller port (Send after commit)
        for cmd in commands_to_send:
            await self.controller.send_command(cmd)

        return outcome or EventOutcome.ACCEPTED, msg

    async def _ticker_loop(self) -> None:
        """Pushes TickInput into inbox every tick_interval_ms."""
        interval_sec = self.config.tick_interval_ms / 1000.0
        while self._running:
            try:
                await asyncio.sleep(interval_sec)
                await self.submit_internal(TickInput())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in ticker loop for {self.junction_id}: {e}")

    async def _housekeeping_loop(self) -> None:
        """Runs periodic cleanup for old tombstones and audit logs."""
        interval_sec = self.app_config.persistence.housekeeping_interval_seconds
        while self._running:
            try:
                await asyncio.sleep(interval_sec)
                now = datetime.now(timezone.utc)
                self.repository.housekeeping_cleanup(
                    retention_days=self.app_config.audit.retention_days,
                    remember_seconds=self.config.events.remember_seconds,
                    now=now,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in housekeeping loop for {self.junction_id}: {e}")
