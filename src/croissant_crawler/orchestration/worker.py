"""Temporal worker: one process polling the main queue, one rate-limited fetch queue per source,
and the LLM queue. Rate limits are enforced server-side per task queue, so they hold globally
no matter how many worker replicas you run."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from .. import db
from ..config import get_settings
from ..sources import all_source_ids
from . import activities as A
from .types import LLM_QUEUE, MAIN_QUEUE, fetch_queue
from .workflows import WORKFLOWS

log = logging.getLogger(__name__)
GRACE = timedelta(seconds=30)


async def connect(retries: int = 30) -> Client:
    s = get_settings()
    for attempt in range(retries):
        try:
            return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
        except Exception as exc:
            log.info("waiting for Temporal at %s (%s)", s.temporal_address, exc)
            await asyncio.sleep(min(2 + attempt, 10))
    raise RuntimeError(f"cannot reach Temporal at {s.temporal_address}")


async def main() -> None:
    s = get_settings()
    await db.migrate()
    client = await connect()
    workers = [
        Worker(client, task_queue=MAIN_QUEUE, workflows=WORKFLOWS, activities=A.MAIN_ACTIVITIES,
               max_concurrent_activities=16, max_concurrent_workflow_tasks=20,
               graceful_shutdown_timeout=GRACE),
    ]
    for sid in all_source_ids():
        rate = s.source_rates.get(sid, 1.0)
        workers.append(Worker(
            client, task_queue=fetch_queue(sid), activities=A.FETCH_ACTIVITIES,
            max_concurrent_activities=max(2, int(rate * 4)),
            max_task_queue_activities_per_second=rate,
            graceful_shutdown_timeout=GRACE,
        ))
    if s.openrouter_api_key:
        workers.append(Worker(
            client, task_queue=LLM_QUEUE, activities=A.LLM_ACTIVITIES,
            max_concurrent_activities=s.llm_concurrency,
            max_task_queue_activities_per_second=s.llm_rate_per_second,
            graceful_shutdown_timeout=GRACE,
        ))
    else:
        log.warning("OPENROUTER_API_KEY not set: LLM enrichment queue disabled")
    # Graceful shutdown on SIGTERM/SIGINT (docker stop, Ctrl-C): stop polling, let in-flight
    # activities finish (up to GRACE), then exit. Without this, killed activities would only be
    # retried after their start-to-close timeout.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runs = [asyncio.create_task(w.run()) for w in workers]
    log.info("worker started: %s", [w.task_queue for w in workers])
    done, _ = await asyncio.wait([asyncio.create_task(stop.wait()), *runs], return_when=asyncio.FIRST_COMPLETED)
    log.info("shutting down workers (grace %ss)", GRACE.total_seconds())
    await asyncio.gather(*(w.shutdown() for w in workers), return_exceptions=True)
    await db.close_pool()
