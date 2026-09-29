import asyncio
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sentinel import worker


def test_worker_initializes_shared_schema_before_starting_scheduler(monkeypatch):
    calls: list[str] = []

    def initialize() -> None:
        calls.append("initialize")

    async def scheduler(stop_event: asyncio.Event) -> None:
        calls.append("scheduler")
        stop_event.set()

    monkeypatch.setattr(worker, "initialize_database", initialize)
    monkeypatch.setattr(worker, "collection_scheduler", scheduler)

    asyncio.run(worker.run_worker())

    assert calls == ["initialize", "scheduler"]
