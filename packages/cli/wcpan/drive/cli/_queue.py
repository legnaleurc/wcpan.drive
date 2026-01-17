from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from contextlib import contextmanager

from wcpan.queue import AioQueue

from ._lib import cerr, cout


class AbstractHandler[S, D](metaclass=ABCMeta):
    @abstractmethod
    async def count_all(self, src: S) -> int:
        raise NotImplementedError

    @abstractmethod
    def source_is_trashed(self, src: S) -> bool:
        raise NotImplementedError

    @abstractmethod
    def source_is_directory(self, src: S) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def do_directory(self, src: S, dst: D) -> D:
        raise NotImplementedError

    @abstractmethod
    async def get_children(self, src: S) -> list[S]:
        raise NotImplementedError

    @abstractmethod
    async def do_file(self, src: S, dst: D) -> None:
        raise NotImplementedError

    @abstractmethod
    def format_source(self, src: S) -> str:
        raise NotImplementedError


class ProgressTracker:
    def __init__(self, total: int) -> None:
        self._total = total
        self._now = 0
        self._error = 0

    @contextmanager
    def collect(self, name: str):
        try:
            yield
        except Exception as e:
            self._error += 1
            cerr(f"[!] {name}, reason: {e}")
            raise
        finally:
            self._now += 1
            cout(f"[{self._now}/{self._total}] {name}")

    @property
    def has_error(self) -> bool:
        return self._error > 0


async def walk_list[S, D](
    handler: AbstractHandler[S, D],
    srcs: Iterable[S],
    dst: D,
    *,
    jobs: int,
    fail_fast: bool,
) -> bool:
    from asyncio import as_completed

    total = 0
    for _ in as_completed(handler.count_all(_) for _ in srcs):
        total += await _
    tracker = ProgressTracker(total)

    with AioQueue[None].fifo() as queue:
        for src in srcs:
            await queue.push(
                _walk_unknown(
                    src,
                    dst,
                    queue=queue,
                    handler=handler,
                    tracker=tracker,
                    fail_fast=fail_fast,
                )
            )
        await queue.consume(jobs)

    return tracker.has_error


async def _walk_unknown[S, D](
    src: S,
    dst: D,
    *,
    queue: AioQueue[None],
    handler: AbstractHandler[S, D],
    tracker: ProgressTracker,
    fail_fast: bool,
) -> None:
    if handler.source_is_trashed(src):
        return
    if handler.source_is_directory(src):
        await queue.push(
            _walk_directory(
                src,
                dst,
                queue=queue,
                handler=handler,
                tracker=tracker,
                fail_fast=fail_fast,
            )
        )
    else:
        await queue.push(
            _walk_file(src, dst, handler=handler, tracker=tracker, fail_fast=fail_fast)
        )


async def _walk_directory[S, D](
    src: S,
    dst: D,
    *,
    queue: AioQueue[None],
    handler: AbstractHandler[S, D],
    tracker: ProgressTracker,
    fail_fast: bool,
) -> None:
    try:
        with tracker.collect(handler.format_source(src)):
            new_directory = await handler.do_directory(src, dst)
            children = await handler.get_children(src)
    except Exception:
        if fail_fast:
            raise
        return

    for child in children:
        await queue.push(
            _walk_unknown(
                child,
                new_directory,
                queue=queue,
                handler=handler,
                tracker=tracker,
                fail_fast=fail_fast,
            )
        )


async def _walk_file[S, D](
    src: S,
    dst: D,
    *,
    handler: AbstractHandler[S, D],
    tracker: ProgressTracker,
    fail_fast: bool,
) -> None:
    try:
        with tracker.collect(handler.format_source(src)):
            await handler.do_file(src, dst)
    except Exception:
        if fail_fast:
            raise
        return
