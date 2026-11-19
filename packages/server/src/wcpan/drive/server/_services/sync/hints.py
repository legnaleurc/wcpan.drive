"""The hint stage: raw change events, handled one at a time per backend.

See "Backend framework" in the package ``SPEC.md``.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from logging import getLogger
from typing import Any

from .writer import WriterStoppedError


_L = getLogger(__name__)


class ChangeHandler[E](ABC):
    """Turns a backend's raw change events into facts.

    Events run one at a time, so reads and writes within ``handle`` happen in
    the order they are written.
    """

    # False: a failure stops the server, as nothing else would repair the mirror.
    continue_after_failure: bool = False

    @abstractmethod
    async def handle(self, event: E) -> None:
        """Act on *event*, after every earlier event has finished."""


class HintStage:
    """One backend's ordered queue of raw events, and its delayed work."""

    def __init__(
        self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """*sleep* waits out ``later`` delays."""
        self._sleep = sleep
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._group: asyncio.TaskGroup | None = None
        self._handler: ChangeHandler[Any] | None = None

    def publish(self, event: object) -> None:
        """Queue *event*; never blocks, and reads nothing."""
        self._queue.put_nowait(event)

    def later(
        self, key: str, delay: float, work: Callable[[], Awaitable[None]]
    ) -> None:
        """Run *work* on its own after *delay*, unless replaced first.

        A newer call with the same *key* replaces a pending timer but never
        cancels work already running. Delayed work does not hold up events or
        ``join``, and its failures follow the handler's policy.

        Raises:
            RuntimeError: If ``run`` is not running.
        """
        if self._group is None:
            raise RuntimeError("delayed work needs a running hint consumer")
        pending = self._timers.pop(key, None)
        if pending is not None:
            pending.cancel()
        self._timers[key] = self._group.create_task(self._wait(key, delay, work))

    async def join(self) -> None:
        """Wait until every published event has been fully handled."""
        await self._queue.join()

    async def run(self, handler: ChangeHandler[Any]) -> None:
        """Handle events until cancelled, or until a failure must stop the server.

        Raises:
            Exception: What failed, unless *handler* continues after failures.
                ``WriterStoppedError`` always stops it.
        """
        self._handler = handler
        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                while True:
                    event = await self._queue.get()
                    try:
                        await self._attempt(lambda: handler.handle(event))
                    finally:
                        self._queue.task_done()
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from failed
        finally:
            self._group = None
            self._handler = None
            self._timers.clear()

    async def _wait(
        self, key: str, delay: float, work: Callable[[], Awaitable[None]]
    ) -> None:
        await self._sleep(delay)
        # Past this point a newer call for the key no longer cancels the work.
        del self._timers[key]
        await self._attempt(work)

    async def _attempt(self, work: Callable[[], Awaitable[None]]) -> None:
        handler = self._handler
        assert handler is not None
        try:
            await work()
        except WriterStoppedError:
            raise
        except Exception:
            if not handler.continue_after_failure:
                raise
            _L.exception("Handling a change failed; continuing")
