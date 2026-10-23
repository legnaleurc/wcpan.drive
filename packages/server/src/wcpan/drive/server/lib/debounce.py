import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


type AsyncFactory = Callable[[], Coroutine[Any, Any, None]]


class Debouncer:
    """Runs delayed work by canceling only the pending timer."""

    def __init__(
        self,
        group: asyncio.TaskGroup,
        delay: float,
    ) -> None:
        self._group = group
        self._delay = delay
        self._timer: asyncio.Task[None] | None = None

    def start(self, factory: AsyncFactory) -> asyncio.Task[None]:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()

        async def timer() -> None:
            await asyncio.sleep(self._delay)
            self._group.create_task(factory())

        self._timer = self._group.create_task(timer())
        return self._timer

    def cancel_pending(self) -> bool:
        timer = self._timer
        if timer is None or timer.done():
            return False
        self._timer = None
        timer.cancel()
        return True

    def is_timer(self, task: asyncio.Task[None]) -> bool:
        return self._timer is task


class TaskIdDebouncer:
    """Debounces work independently for each task id."""

    def __init__(
        self,
        group: asyncio.TaskGroup,
        delay: float,
    ) -> None:
        self._group = group
        self._delay = delay
        self._debouncers: dict[str, Debouncer] = {}

    def schedule(
        self,
        task_id: str,
        factory: AsyncFactory,
    ) -> None:
        debouncer = self._debouncers.get(task_id)
        if debouncer is None:
            debouncer = Debouncer(self._group, self._delay)
            self._debouncers[task_id] = debouncer

        timer = debouncer.start(factory)

        def cleanup(done: asyncio.Task[None]) -> None:
            current = self._debouncers.get(task_id)
            if current is not None and current.is_timer(done):
                self._debouncers.pop(task_id, None)

        timer.add_done_callback(cleanup)

    def cancel(self, task_id: str) -> None:
        if current := self._debouncers.pop(task_id, None):
            current.cancel_pending()
