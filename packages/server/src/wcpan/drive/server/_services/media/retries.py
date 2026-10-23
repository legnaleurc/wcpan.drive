"""Backoff retries of media probes that failed for a transient reason.

Timers live in memory, keyed by backend instance and source key, never public
ID. A restart retries through each backend's startup pass instead. A probe of a
file that starts cancels its pending timer, and the probe's own failure
schedules the next one. A retry only submits the file for probing, so another
observation submitted meanwhile may still probe it once more; ordering keeps
the later result.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from logging import getLogger


_L = getLogger(__name__)

type RetryMedia = Callable[[str], Awaitable[None]]
"""Observe the file with a source key again under a fresh read, and submit it
for probing; raises when the attempt cannot be made now."""

# Seconds before the first retry, doubled for each failure up to the cap.
FIRST_DELAY = 30.0
MAX_DELAY = 60.0 * 60.0


class MediaRetries:
    """Retry timers for the backends that registered how to retry."""

    def __init__(
        self, *, first_delay: float = FIRST_DELAY, max_delay: float = MAX_DELAY
    ) -> None:
        self._first_delay = first_delay
        self._max_delay = max_delay
        self._retry: dict[str, RetryMedia] = {}
        self._timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._running: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Timers that fired while their previous attempt still ran.
        self._again: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def register(self, backend: str, retry: RetryMedia) -> AsyncGenerator[None]:
        """Retry *backend*'s transient failures with *retry* until the context exits.

        On exit, pending timers are cancelled and running attempts awaited.
        """
        if backend in self._retry:
            raise ValueError(f"backend {backend!r} already registered retries")
        self._retry[backend] = retry
        try:
            yield
        finally:
            del self._retry[backend]
            for key in [k for k in self._timers if k[0] == backend]:
                self._timers.pop(key).cancel()
            running = [t for k, t in self._running.items() if k[0] == backend]
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for key in [k for k in self._failures if k[0] == backend]:
                del self._failures[key]
            self._again -= {k for k in self._again if k[0] == backend}

    def failed(self, backend: str, source_key: str) -> float | None:
        """Schedule a retry after another transient failure; returns the delay.

        ``None`` when *backend* registered no retries, as offline.
        """
        if backend not in self._retry:
            return None
        key = (backend, source_key)
        count = self._failures.get(key, 0)
        self._failures[key] = count + 1
        delay = min(self._first_delay * 2**count, self._max_delay)
        self._cancel_timer(key)
        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(delay, self._fire, key)
        return delay

    def attempting(self, backend: str, source_key: str) -> None:
        """An attempt at the file starts; its pending timer is not needed."""
        self._cancel_timer((backend, source_key))
        self._again.discard((backend, source_key))

    def forget(self, backend: str, source_key: str) -> None:
        """The file is applied, removed, or failed for a lasting reason."""
        key = (backend, source_key)
        self._cancel_timer(key)
        self._failures.pop(key, None)
        self._again.discard(key)

    def retain(self, backend: str, source_keys: set[str]) -> None:
        """Forget *backend*'s files other than *source_keys*, as records were pruned."""
        tracked = {*self._timers, *self._failures}
        for key in tracked:
            if key[0] == backend and key[1] not in source_keys:
                self.forget(*key)

    def pending(self, backend: str) -> set[str]:
        """Source keys of *backend* with a scheduled or running retry."""
        return {k[1] for k in (*self._timers, *self._running) if k[0] == backend}

    def _cancel_timer(self, key: tuple[str, str]) -> None:
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _fire(self, key: tuple[str, str]) -> None:
        self._timers.pop(key, None)
        if key[0] not in self._retry:
            return
        if key in self._running:
            # The running attempt may have scheduled this timer itself (a
            # folder scan that probed the file); run again once it is done.
            self._again.add(key)
            return
        task = asyncio.create_task(self._attempt(key))
        self._running[key] = task
        task.add_done_callback(lambda _: self._finished(key))

    def _finished(self, key: tuple[str, str]) -> None:
        self._running.pop(key, None)
        if key in self._again:
            self._again.discard(key)
            self._fire(key)

    async def _attempt(self, key: tuple[str, str]) -> None:
        backend, source_key = key
        retry = self._retry.get(backend)
        if retry is None:
            return
        try:
            await retry(source_key)
        except Exception:
            _L.warning(
                "Could not retry the media probe of %s; retrying later",
                source_key,
                exc_info=True,
            )
            self.failed(backend, source_key)
