import asyncio
import logging
from typing import Dict, List, Optional
from backend.adapters.persistence.repository import SQLiteRepository
from backend.application.junction_worker import JunctionWorker
from backend.config.config import AppConfig, JunctionDefinition

logger = logging.getLogger(__name__)


class JunctionManager:
    """Coordinates active junction workers and manages life-cycle."""

    def __init__(self, app_config: AppConfig, repository: SQLiteRepository):
        self.app_config = app_config
        self.repository = repository
        self.workers: Dict[str, JunctionWorker] = {}

    async def initialize(self) -> None:
        """Discovers and starts workers for all configured and persisted junctions."""
        # Configured junctions
        for j_id in self.app_config.junctions:
            if j_id not in self.workers:
                worker = JunctionWorker(j_id, self.app_config, self.repository)
                self.workers[j_id] = worker
                await worker.start()

        # Any additional junctions stored in database
        db_junction_ids = self.repository.list_junction_ids()
        for j_id in db_junction_ids:
            if j_id not in self.workers:
                worker = JunctionWorker(j_id, self.app_config, self.repository)
                self.workers[j_id] = worker
                await worker.start()

    async def shutdown(self) -> None:
        """Stops all junction workers."""
        for worker in self.workers.values():
            await worker.stop()
        self.workers.clear()

    def get_worker(self, junction_id: str) -> Optional[JunctionWorker]:
        return self.workers.get(junction_id)

    async def register_junction(self, junction_id: str, definition: JunctionDefinition) -> JunctionWorker:
        """Registers a new junction dynamically (e.g. via POST /api/junctions)."""
        if junction_id in self.workers:
            raise ValueError(f"Junction '{junction_id}' already exists")

        self.app_config.junctions[junction_id] = definition
        worker = JunctionWorker(junction_id, self.app_config, self.repository)
        self.workers[junction_id] = worker
        await worker.start()
        return worker

    def list_junction_ids(self) -> List[str]:
        return list(self.workers.keys())
