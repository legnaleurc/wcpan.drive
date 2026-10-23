import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable


_DEFAULT_CONCURRENCY = os.process_cpu_count() or 1


async def parallel_bfs[T](
    initial: Iterable[T],
    visit: Callable[[T], Awaitable[list[T]]],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> None:
    """BFS with N concurrent workers.

    `visit(item)` processes the item and returns child items to enqueue.
    It must not raise — handle errors internally and return [] for failed items.
    """
    queue: asyncio.Queue[T] = asyncio.Queue()
    for item in initial:
        queue.put_nowait(item)

    async with asyncio.TaskGroup() as group:
        workers = [
            group.create_task(_worker(queue=queue, visit=visit))
            for _ in range(concurrency)
        ]
        await queue.join()
        for w in workers:
            w.cancel()


async def _worker[T](
    *, queue: asyncio.Queue[T], visit: Callable[[T], Awaitable[list[T]]]
) -> None:
    while True:
        item = await queue.get()
        try:
            children = await visit(item)
            for child in children:
                queue.put_nowait(child)
        finally:
            queue.task_done()
