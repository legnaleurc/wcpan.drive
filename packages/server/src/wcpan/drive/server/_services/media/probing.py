"""Media probing of backend files, through the sources backends register.

A backend registers how to open a ``MediaSource`` for one of its observations
while it is connected; probes of its files fail as transient otherwise.
"""

import asyncio
from collections.abc import Callable, Generator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager

from ..._types import NodeObservation, ProbeFailure
from .probe.runner import ProbeRunner
from .probe.source import (
    MediaProbeError,
    MediaSource,
    ProbeUnavailableError,
)


type OpenMediaSource = Callable[
    [NodeObservation], AbstractAsyncContextManager[MediaSource]
]
"""Opens the file an observation describes, bound to the state it observed.

Raises when the file cannot be opened or no longer matches the observation.
"""


# Probes of one backend's files running at once.
BACKEND_ADMISSION = 2


class ProbesNotDeclaredError(ProbeUnavailableError):
    """A command that did not declare probing tried to probe.

    A ``ProbeUnavailableError``, so it stops the command instead of failing
    one file.
    """


class MediaProbing:
    """Probes files through the runner, one backend's sources at a time.

    Without a runner (a maintenance command that does not probe), sources can
    still be registered, but every probe raises ``ProbesNotDeclaredError``.
    """

    def __init__(
        self, *, runner: ProbeRunner | None, admission: int = BACKEND_ADMISSION
    ) -> None:
        self._runner = runner
        self._admission = admission
        self._sources: dict[str, OpenMediaSource] = {}
        self._slots: dict[str, asyncio.Semaphore] = {}

    @contextmanager
    def register(self, backend: str, open_source: OpenMediaSource) -> Generator[None]:
        """Probe *backend*'s files through *open_source* until the context exits."""
        if backend in self._sources:
            raise ValueError(f"backend {backend!r} already registered a media source")
        self._sources[backend] = open_source
        self._slots[backend] = asyncio.Semaphore(self._admission)
        try:
            yield
        finally:
            del self._sources[backend]

    async def probe(self, observation: NodeObservation) -> tuple[int, int]:
        """Width and height of the file *observation* describes.

        Raises:
            MediaProbeError: The probe failed; its kind decides the retry.
            ProbeUnavailableError: MediaInfo cannot be run, or this command
                does not probe (``ProbesNotDeclaredError``).
        """
        runner = self._require_runner()
        backend = observation.backend
        open_source = self._sources.get(backend) if backend is not None else None
        if backend is None or open_source is None:
            raise MediaProbeError(
                ProbeFailure.TRANSIENT, f"backend {backend!r} has no media source"
            )
        record = observation.record
        async with self._slots[backend], AsyncExitStack() as stack:
            try:
                source = await stack.enter_async_context(open_source(observation))
            except Exception as e:
                raise MediaProbeError(
                    ProbeFailure.TRANSIENT, f"cannot open the file: {e}"
                ) from e
            return await runner.probe(source, is_image=record.is_image)

    async def check(self) -> None:
        """Check MediaInfo now; raises ``ProbeUnavailableError`` if it cannot probe."""
        await self._require_runner().check()

    def _require_runner(self) -> ProbeRunner:
        if self._runner is None:
            raise ProbesNotDeclaredError(
                "this command does not probe media, but a file needs probing"
            )
        return self._runner

    async def probe_source(
        self, source: MediaSource, *, is_image: bool
    ) -> tuple[int, int]:
        """Width and height of the media in *source*, such as an upload's bytes.

        Raises:
            MediaProbeError: The probe failed; its kind says why.
            ProbeUnavailableError: MediaInfo cannot be run, or this command
                does not probe (``ProbesNotDeclaredError``).
        """
        return await self._require_runner().probe(source, is_image=is_image)
