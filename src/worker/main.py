import asyncio
import logging
import os
import signal
import socket
from uuid import uuid4

from src.application.services.process_ingest_job import ProcessIngestJob
from src.bootstrap import AppContext, build_context
from src.config import Settings, get_settings
from src.logging_setup import configure_logging
from src.worker.runner import Worker

logger = logging.getLogger(__name__)


async def main() -> int:
    """The worker process; returns its exit code."""
    settings = get_settings()
    configure_logging(
        level=settings.log_level, log_format=settings.log_format, version=settings.git_sha
    )
    # Before anything is built: a SIGTERM during startup then still closes what was created.
    stop = stop_on_signals()
    return await run(settings, stop)


def stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    return stop


async def run(settings: Settings, stop: asyncio.Event) -> int:
    """Shutdown order: stop claiming, finish or release in-flight jobs (`Worker.run`), then close
    the context. It's closed however `Worker.run` ends, also when it raises."""
    try:
        context = await build_context(settings)
    except Exception:
        logger.exception("worker failed to start")
        return 1
    try:
        await create_worker(context, worker_id=new_worker_id()).run(stop)
    finally:
        await context.aclose()
    return 0


def create_worker(context: AppContext, *, worker_id: str) -> Worker:
    settings = context.settings
    process = ProcessIngestJob(
        worker_id=worker_id,
        document_reader=context.document_reader,
        qr_parser=context.qr_parser,
        llm_parser=context.llm_parser,
        llm_quota=context.llm_quota,
        llm_failure_cache=context.llm_failure_cache,
        unit_of_work=context.unit_of_work,
        clock=context.clock,
        retry_policy=context.retry_policy,
    )
    return Worker(
        worker_id=worker_id,
        unit_of_work=context.unit_of_work,
        process=process.execute,
        lease_seconds=settings.job_lease_seconds,
        max_attempts=context.retry_policy.max_attempts,
        concurrency=settings.worker_concurrency,
        poll_interval=settings.worker_poll_interval_seconds,
        shutdown_timeout=settings.worker_shutdown_timeout_seconds,
    )


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"
