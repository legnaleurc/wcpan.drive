"""Probe media with the system MediaInfo executable, reading over loopback HTTP.

Each probe runs ``mediainfo --Output=JSON URL`` as a subprocess, where the URL
names the probe's source on the ``ProbeSourceService``. MediaInfo's own reader
decides which ranges to fetch, so a probe may read a whole file; only the
number of probes at once and each probe's deadline are bounded.

MediaInfo's exit status and output are not enough: it has returned dimensions
after interrupted transfers. A probe succeeds only if its session recorded no
failed read and the source still matches its observation afterwards.
"""

import asyncio
import json
import os
import re
import struct
import zlib
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from logging import getLogger
from typing import cast

from ...._types import ProbeFailure
from .service import ProbeSourceService
from .source import (
    MediaProbeError,
    MediaSource,
    MediaSourceError,
    ProbeUnavailableError,
)


_L = getLogger(__name__)

# The MediaInfo command; arguments are appended.
DEFAULT_COMMAND = ("mediainfo",)
# The oldest MediaInfoLib accepted. 25.04 is the tested one; older releases fail
# on some WMV files and report no WebP dimensions.
MIN_VERSION = (25, 4)
# Probes run at once, whatever the CPU count: they mostly wait for bytes.
DEFAULT_WORKERS = 2
# Seconds to wait for a killed MediaInfo to exit.
_REAP_TIMEOUT = 5.0

_VERSION = re.compile(rb"MediaInfoLib - v(\d+)\.(\d+)")


@dataclass(frozen=True, kw_only=True)
class ProbeLimits:
    """Time limits of one probe."""

    # Seconds for the whole probe. Exhausted while MediaInfo waits for the
    # source, it is ``TRANSIENT``; otherwise ``LIMIT``.
    probe_timeout: float = 300.0
    # Seconds for each read from the source, and for rechecking the source
    # afterwards; a timeout is ``TRANSIENT``.
    read_timeout: float = 30.0
    # Seconds for ``mediainfo --Version`` at startup.
    start_timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class _Run:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProbeRunner:
    """Runs probes as MediaInfo subprocesses, a bounded number at a time.

    Use as an async context manager, which starts the source service before any
    probe and kills running subprocesses on exit. ``check`` verifies the
    executable once, so a missing or incompatible install fails at startup.
    """

    def __init__(
        self,
        *,
        workers: int = DEFAULT_WORKERS,
        limits: ProbeLimits | None = None,
        command: Sequence[str] = DEFAULT_COMMAND,
    ) -> None:
        self._slots = asyncio.Semaphore(workers)
        self._limits = limits or ProbeLimits()
        self._command = list(command)
        self._service = ProbeSourceService(read_timeout=self._limits.read_timeout)
        self._running: set[asyncio.subprocess.Process] = set()
        self._closed = False
        # The result of the first check, shared by every later probe.
        self._checked: asyncio.Future[None] | None = None

    async def __aenter__(self) -> "ProbeRunner":
        await self._service.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def check(self) -> None:
        """Check the executable's version and probe a fixture through the service.

        Runs once; every probe waits for it first, so offline commands are
        checked too. Raises ``ProbeUnavailableError`` if MediaInfo is missing,
        too old, or cannot read over HTTP.
        """
        if self._checked is None:
            self._checked = asyncio.ensure_future(self._check())
        await asyncio.shield(self._checked)

    async def _check(self) -> None:
        version = await self._version()
        if version < MIN_VERSION:
            raise ProbeUnavailableError(
                f"MediaInfoLib {_format(version)} is older than {_format(MIN_VERSION)}"
            )
        try:
            dimensions = await self._probe(_Fixture())
        except MediaProbeError as e:
            raise ProbeUnavailableError(
                f"MediaInfo cannot probe over HTTP: {e.reason}"
            ) from e
        if dimensions != _FIXTURE_DIMENSIONS:
            raise ProbeUnavailableError(
                f"MediaInfo probed {dimensions}, expected {_FIXTURE_DIMENSIONS}"
            )
        _L.info("Probing with MediaInfoLib %s", _format(version))

    async def probe(self, source: MediaSource, *, is_image: bool) -> tuple[int, int]:
        """Width and height of the media in *source*, both positive.

        *is_image* is accepted for callers' symmetry; MediaInfo detects the
        format itself.

        Raises:
            MediaProbeError: The probe failed; its kind decides the retry.
            ProbeUnavailableError: MediaInfo cannot be run.
        """
        await self.check()
        return await self._probe(source)

    async def _probe(self, source: MediaSource) -> tuple[int, int]:
        async with self._slots:
            if self._closed:
                raise ProbeUnavailableError("probing has stopped")
            fd = _local_fd(source)
            if fd is not None:
                width, height = await self._probe_local(fd)
            else:
                width, height = await self._probe_served(source)
        await self._check_unchanged(source)
        if width <= 0 or height <= 0:
            raise MediaProbeError(
                ProbeFailure.INVALID, f"no complete dimensions ({width}x{height})"
            )
        return width, height

    async def _probe_local(self, fd: int) -> tuple[int, int]:
        """Probe ``/dev/fd/N`` of a descriptor the backend opened and verified.

        MediaInfo opens the same inode, whatever happened to the path since.
        The server sees none of its reads: a short read within the observed
        size means truncation, which the recheck catches, but a local I/O
        error MediaInfo takes for the end of the file goes undetected.
        """
        target = f"/dev/fd/{fd}"
        run, _ = await self._run_until_deadline(
            target, pass_fds=(fd,), waiting=lambda: False
        )
        if run is None:
            raise MediaProbeError(
                ProbeFailure.LIMIT,
                f"took more than {self._limits.probe_timeout} seconds",
            )
        return _parse(run, url=target)

    async def _probe_served(self, source: MediaSource) -> tuple[int, int]:
        """Probe *source* through the source service."""
        async with self._service.session(source) as session:
            run, waiting = await self._run_until_deadline(
                session.url,
                pass_fds=(),
                waiting=lambda: session.reading or session.timed_out,
            )
        # The session has ended: no read is in flight, and the record of
        # failed reads is final.
        if session.failures:
            raise MediaProbeError(
                ProbeFailure.TRANSIENT, f"the source failed: {session.failures[0]}"
            )
        if run is None:
            kind = ProbeFailure.TRANSIENT if waiting else ProbeFailure.LIMIT
            raise MediaProbeError(
                kind, f"took more than {self._limits.probe_timeout} seconds"
            )
        return _parse(run, url=session.url)

    async def close(self) -> None:
        """Kill running probes, which then fail, and stop the source service."""
        self._closed = True
        if self._checked is not None and not self._checked.done():
            # Its own cleanup kills a ``--Version`` still running.
            self._checked.cancel()
            await asyncio.gather(self._checked, return_exceptions=True)
        running = list(self._running)
        for proc in running:
            _kill(proc)
        await asyncio.gather(*(_reap(proc) for proc in running))
        await self._service.close()

    async def _run_until_deadline(
        self,
        target: str,
        *,
        pass_fds: tuple[int, ...],
        waiting: Callable[[], bool],
    ) -> tuple[_Run | None, bool]:
        """MediaInfo's result, or ``None`` at the deadline with *waiting()*.

        *waiting* is sampled when the deadline passes, before MediaInfo is
        killed, since a read can finish while it is reaped.
        """
        task = asyncio.ensure_future(self._run(target, pass_fds=pass_fds))
        try:
            done, _ = await asyncio.wait({task}, timeout=self._limits.probe_timeout)
            if done:
                return task.result(), False
            return None, waiting()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _run(self, target: str, *, pass_fds: tuple[int, ...]) -> _Run:
        args = [*self._command, "--Output=JSON", "--File_TestContinuousFileNames=0"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                target,
                pass_fds=pass_fds,
                env=_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            raise ProbeUnavailableError(f"cannot run {self._command[0]}: {e}") from e
        except OSError as e:
            raise MediaProbeError(
                ProbeFailure.TRANSIENT, f"cannot start {self._command[0]}: {e}"
            ) from e
        self._running.add(proc)
        if self._closed:
            # Spawned while ``close`` ran; it may have missed this one.
            _kill(proc)
        try:
            stdout, stderr = await proc.communicate()
        finally:
            self._running.discard(proc)
            if proc.returncode is None:
                _kill(proc)
                await _reap(proc)
        assert proc.returncode is not None
        return _Run(proc.returncode, stdout, stderr)

    async def _version(self) -> tuple[int, int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                "--Version",
                env=_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            raise ProbeUnavailableError(f"cannot run {self._command[0]}: {e}") from e
        try:
            async with asyncio.timeout(self._limits.start_timeout):
                output, _ = await proc.communicate()
        except TimeoutError as e:
            raise ProbeUnavailableError(f"{self._command[0]} --Version hung") from e
        finally:
            if proc.returncode is None:
                _kill(proc)
                await _reap(proc)
        match = _VERSION.search(output)
        if proc.returncode != 0 or match is None:
            raise ProbeUnavailableError(
                f"{self._command[0]} --Version gave no MediaInfoLib version"
                f" (exit {proc.returncode})"
            )
        return int(match[1]), int(match[2])

    async def _check_unchanged(self, source: MediaSource) -> None:
        try:
            async with asyncio.timeout(self._limits.read_timeout):
                unchanged = await source.unchanged()
        except Exception as e:
            # Only a source's own errors are worded to be logged; others may
            # carry upstream URLs with credentials.
            detail = str(e) if isinstance(e, MediaSourceError) else type(e).__name__
            raise MediaProbeError(
                ProbeFailure.TRANSIENT, f"cannot recheck the source: {detail}"
            ) from None
        if not unchanged:
            raise MediaProbeError(ProbeFailure.TRANSIENT, "the file changed")


def _parse(run: _Run, *, url: str) -> tuple[int, int]:
    """Dimensions from MediaInfo's JSON, 0 when absent.

    A nonzero exit, unreadable output, or an error MediaInfo reports is a
    failure of the tool or of reading, not evidence about the media.
    """
    if run.returncode != 0:
        how = (
            f"killed by signal {-run.returncode}"
            if run.returncode < 0
            else f"exit {run.returncode}"
        )
        raise MediaProbeError(
            ProbeFailure.TRANSIENT, f"MediaInfo failed ({how}){_errors(run, url)}"
        )
    try:
        output = json.loads(run.stdout)
    except ValueError as e:
        raise MediaProbeError(
            ProbeFailure.TRANSIENT, f"MediaInfo output is not JSON: {e}"
        ) from e
    if not isinstance(output, dict) or "media" not in output:
        raise MediaProbeError(ProbeFailure.TRANSIENT, "MediaInfo output has no media")
    errors = _errors(run, url)
    if errors:
        # For example the URL could not be read; MediaInfo still exits 0.
        raise MediaProbeError(ProbeFailure.TRANSIENT, f"MediaInfo reported{errors}")
    return _dimensions(cast(dict[str, object], output)["media"])


def _dimensions(media: object) -> tuple[int, int]:
    """The last video or image track's width and height, 0 when missing."""
    width = 0
    height = 0
    if not isinstance(media, dict):
        return width, height
    tracks = cast(dict[str, object], media).get("track")
    if not isinstance(tracks, list):
        return width, height
    for track in cast(list[object], tracks):
        if not isinstance(track, dict):
            continue
        fields = cast(dict[str, object], track)
        if fields.get("@type") in ("Video", "Image"):
            width = _int(fields.get("Width"))
            height = _int(fields.get("Height"))
    return width, height


def _int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def _errors(run: _Run, url: str) -> str:
    """MediaInfo's error lines, without the URL's token."""
    lines = [
        line.replace(url, "<source>")
        for line in run.stderr.decode("utf-8", "replace").splitlines()
        if line.startswith("E:")
    ]
    return f": {'; '.join(lines)[:500]}" if lines else ""


def _environment() -> dict[str, str]:
    """The server's environment without proxy settings.

    MediaInfo's libcurl reads ``http_proxy``, ``all_proxy`` and the like even
    for loopback URLs, so a proxy set for the server would receive the probe's
    URL and token, and probes would hang. MediaInfo only reads the loopback
    service or ``/dev/fd``, and the server's own HTTP clients ignore these
    variables too.
    """
    return {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}


def _local_fd(source: MediaSource) -> int | None:
    """The source's own descriptor, if it offers one (``local_fd``)."""
    local_fd = getattr(source, "local_fd", None)
    if not callable(local_fd):
        return None
    fd = local_fd()
    return fd if isinstance(fd, int) else None


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), _REAP_TIMEOUT)
    except TimeoutError:
        _L.error("MediaInfo %d did not exit after being killed", proc.pid)


def _format(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]:02d}"


_FIXTURE_DIMENSIONS = (3, 2)


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    pixels = zlib.compress(b"".join(b"\0" + b"\0" * width for _ in range(height)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


class _Fixture:
    """A small PNG for the startup check."""

    def __init__(self) -> None:
        self._data = _png(*_FIXTURE_DIMENSIONS)

    @property
    def size(self) -> int:
        return len(self._data)

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        yield self._data[offset : offset + length]

    async def unchanged(self) -> bool:
        return True
