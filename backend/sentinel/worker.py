"""Separately supervised Sentinel worker for schedule evaluation and dispatch."""

from __future__ import annotations

import asyncio
import signal
import time

import psycopg

from .bootstrap import initialize_database
from .scheduling import collection_scheduler


async def run_worker() -> None:
    """Start the single schedule authority after the shared schema is ready."""

    for attempt in range(20):
        try:
            initialize_database()
            break
        except psycopg.OperationalError:
            if attempt == 19:
                raise
            time.sleep(1)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows event loops
            pass
    await collection_scheduler(stop_event)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
