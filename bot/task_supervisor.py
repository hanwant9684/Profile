"""Helpers for supervising application-owned asyncio tasks and futures."""

import asyncio
import concurrent.futures
import logging
from typing import Awaitable, Coroutine, Set

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()


def _log_task_result(task: asyncio.Task, name: str) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    try:
        exception = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Could not inspect background task %s", name)
        return
    if exception is not None:
        logger.error(
            "Background task %s failed",
            name,
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def create_background_task(
    awaitable: Awaitable,
    *,
    name: str,
) -> asyncio.Task:
    """Create a task that is retained and reports uncaught exceptions."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(awaitable, name=name)
    _background_tasks.add(task)
    task.add_done_callback(lambda completed: _log_task_result(completed, name))
    return task


def _log_future_result(
    future: concurrent.futures.Future,
    name: str,
) -> None:
    if future.cancelled():
        return
    try:
        exception = future.exception()
    except concurrent.futures.CancelledError:
        return
    except Exception:
        logger.exception("Could not inspect scheduled future %s", name)
        return
    if exception is not None:
        logger.error(
            "Scheduled future %s failed",
            name,
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def schedule_coroutine_threadsafe(
    coroutine: Coroutine,
    loop: asyncio.AbstractEventLoop,
    *,
    name: str,
) -> concurrent.futures.Future:
    """Schedule a coroutine from another thread and report its result."""
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception:
        coroutine.close()
        raise
    future.add_done_callback(lambda completed: _log_future_result(completed, name))
    return future


async def cancel_background_tasks() -> None:
    """Cancel and await all detached tasks owned by the application."""
    current = asyncio.current_task()
    tasks = [
        task for task in _background_tasks
        if task is not current and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _background_tasks.clear()