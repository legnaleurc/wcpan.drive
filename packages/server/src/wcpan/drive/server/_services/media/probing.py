"""Media probing of backend files, through the sources backends register.

A backend registers how to open a ``MediaSource`` for one of its observations
while it is connected; probes of its files fail as transient otherwise.
"""

import asyncio
from collections.abc import Callable, Generator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from dataclasses import replace

from wcpan.drive.protocol.lib import guess_mime_type
from wcpan.drive.protocol.types import MediaInfo

from ..._types import NodeObservation
from ..upload.service import UploadInvalidMediaError, UploadTransientError
from .probe.runner import ProbeRunner
from .probe.source import (
    MediaProbeError,
    MediaSource,
    ProbeFailure,
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

    async def probe_upload(self, source: MediaSource, kind: MediaInfo) -> MediaInfo:
        """*kind* with the dimensions probed from an upload's bytes in *source*.

        Probed before the file is stored, so invalid media stores nothing.

        Raises:
            UploadInvalidMediaError: The bytes are not readable media.
            UploadTransientError: The probe failed for another reason; the
                upload can be finished again.
        """
        runner = self._require_runner()
        try:
            width, height = await runner.probe(source, is_image=kind.is_image)
        except MediaProbeError as e:
            if e.kind == ProbeFailure.INVALID:
                raise UploadInvalidMediaError(
                    f"Cannot read the media: {e.reason}"
                ) from e
            raise UploadTransientError(f"Cannot probe the media now: {e.reason}") from e
        return replace(kind, width=width, height=height)


def upload_probe_kind(name: str, media_info: MediaInfo | None) -> MediaInfo | None:
    """What an upload named *name* must be probed as, or ``None``.

    Complete client media info is used as given, as is client media info that
    says the file is not media. Without any, the kind is guessed from the name,
    as the filesystem backend classifies files.
    """
    if media_info is not None:
        if not (media_info.is_image or media_info.is_video):
            return None
        if media_info.width > 0 and media_info.height > 0:
            return None
        return replace(media_info, width=0, height=0)
    mime_type = guess_mime_type(name, is_directory=False)
    if mime_type.startswith("image/"):
        return MediaInfo(is_image=True)
    if mime_type.startswith("video/"):
        return MediaInfo(is_video=True)
    return None
