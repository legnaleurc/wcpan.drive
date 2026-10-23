"""ProbeRunner: probing with the MediaInfo executable over loopback HTTP."""

import asyncio
import os
import sys
import tempfile
import textwrap
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from wcpan.drive.server._services.media.probe.runner import ProbeLimits, ProbeRunner
from wcpan.drive.server._services.media.probe.source import (
    MediaProbeError,
    MediaSourceError,
    ProbeFailure,
    ProbeUnavailableError,
)

from ._media import MEDIA as _MEDIA
from ._media import png as _png
from ._media import requires_mediainfo


class _Bytes:
    """A MediaSource over bytes, in 4 KiB chunks, recording streams."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.streams: list[tuple[int, int]] = []
        self.changed = False

    @property
    def size(self) -> int:
        return len(self.data)

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        self.streams.append((offset, length))
        end = offset + length
        for start in range(offset, end, 4096):
            yield self.data[start : min(start + 4096, end)]
            await asyncio.sleep(0)

    async def unchanged(self) -> bool:
        return not self.changed


class _Failing(_Bytes):
    """Fails after the first chunk of every stream."""

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        yield self.data[offset : offset + min(length, 4096)]
        raise MediaSourceError("connection reset")


class _Stalled(_Bytes):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.started = asyncio.Event()

    async def stream(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        self.started.set()
        await asyncio.Event().wait()
        yield b""


# A stand-in for the mediainfo executable: the mode comes first, then the
# runner's arguments, the URL last.
_FAKE = textwrap.dedent(
    """
    import json, os, signal, sys, time, urllib.request
    mode = sys.argv[1]
    if sys.argv[2] == "--Version":
        if mode == "hang-version":
            time.sleep(60)
        version = mode.split("=", 1)[1] if mode.startswith("version=") else "25.04"
        print("MediaInfo Command line,")
        print(f"MediaInfoLib - v{version}")
        sys.exit(0)
    url = sys.argv[-1]
    def track(width, height):
        return {"@type": "Video", "Width": str(width), "Height": str(height)}
    def emit(media):
        print(json.dumps({"creatingLibrary": {"name": "MediaInfoLib"}, "media": media}))
    if mode == "read-all":
        # Read the whole file, as MediaInfo does for most formats.
        with urllib.request.urlopen(url) as r:
            r.read()
        emit({"track": [{"@type": "General"}, track(7, 5)]})
    elif mode == "read-head":
        # Read the start, then close the response early, as a seek does.
        with urllib.request.urlopen(url) as r:
            r.read(10)
        emit({"track": [{"@type": "General"}, track(7, 5)]})
    elif mode == "read-ignoring-errors":
        # Report dimensions whatever the transfer did, as MediaInfo has.
        try:
            with urllib.request.urlopen(url) as r:
                r.read()
        except Exception:
            pass
        emit({"track": [{"@type": "General"}, track(7, 5)]})
    elif mode == "tracks":
        emit({"track": [
            {"@type": "General"},
            {"@type": "Image", "Width": "1", "Height": "1"},
            track(640, 480),
            {"@type": "Audio"},
        ]})
    elif mode == "no-tracks":
        emit({"track": [{"@type": "General"}]})
    elif mode == "zero":
        emit({"track": [track(0, 480)]})
    elif mode == "null":
        emit(None)
    elif mode == "error":
        print(f"E: {url}, 404: Not Found", file=sys.stderr)
        emit(None)
    elif mode == "garbage":
        print("not json")
    elif mode == "exit":
        sys.exit(3)
    elif mode == "crash":
        os.kill(os.getpid(), signal.SIGKILL)
    elif mode == "hang":
        time.sleep(60)
    elif mode == "hang-reading":
        urllib.request.urlopen(url).read()
    elif mode == "read-fd":
        # Read the descriptor's file as MediaInfo would; the server's offset
        # must not matter.
        with open(url, "rb") as f:
            data = f.read()
        emit({"track": [track(len(data), 1 if url.startswith("/dev/fd/") else 0)]})
    elif mode == "env":
        # Report the proxy variables MediaInfo was started with, as a width.
        found = [k for k in os.environ if k.lower().endswith("_proxy")]
        emit({"track": [track(len(found) + 1, 1)]})
    elif mode == "flood":
        sys.stderr.write("x" * (1 << 20))
        emit({"track": [track(7, 5)]})
    """
)


class _LocalFile(_Bytes):
    """A source over a local file that offers its descriptor."""

    def __init__(self, path: Path) -> None:
        super().__init__(path.read_bytes())
        self.file = path.open("rb")

    def local_fd(self) -> int | None:
        return self.file.fileno()


class TestFakeMediaInfo(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.script = tmp / "mediainfo.py"
        self.script.write_text(_FAKE)

    async def runner(
        self, mode: str, *, limits: ProbeLimits | None = None, checked: bool = True
    ) -> ProbeRunner:
        runner = await self.enterAsyncContext(
            ProbeRunner(
                workers=1,
                limits=limits or ProbeLimits(probe_timeout=10, read_timeout=5),
                command=[sys.executable, str(self.script), mode],
            )
        )
        if checked:
            # Most modes cannot probe the startup fixture; skip the check.
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            runner._checked = done  # pyright: ignore[reportPrivateUsage]
        return runner

    async def failure(
        self, mode: str, source: _Bytes | None = None, **kwargs: object
    ) -> MediaProbeError:
        runner = await self.runner(mode, **kwargs)  # type: ignore[arg-type]
        with self.assertRaises(MediaProbeError) as cm:
            await runner.probe(source or _Bytes(b"x" * 100), is_image=False)
        return cm.exception

    async def test_dimensions_after_reading_the_whole_file(self) -> None:
        # given
        runner = await self.runner("read-all")
        source = _Bytes(b"x" * 100_000)
        # when
        dims = await runner.probe(source, is_image=False)
        # then
        self.assertEqual(dims, (7, 5))
        self.assertEqual(source.streams, [(0, 100_000)])

    async def test_closing_a_response_early_is_not_a_failure(self) -> None:
        # given
        runner = await self.runner("read-head")
        # when / then
        self.assertEqual(
            await runner.probe(_Bytes(b"x" * 1_000_000), is_image=False), (7, 5)
        )

    async def test_last_video_or_image_track_wins(self) -> None:
        # given
        runner = await self.runner("tracks")
        # when / then
        self.assertEqual(await runner.probe(_Bytes(b"x"), is_image=False), (640, 480))

    async def test_failed_read_is_transient_even_with_dimensions(self) -> None:
        # when
        with self.assertLogs("aiohttp.server", "ERROR"):
            error = await self.failure("read-ignoring-errors", _Failing(b"x" * 100_000))
        # then
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)
        self.assertIn("connection reset", error.reason)

    async def test_file_changed_during_the_probe_is_transient(self) -> None:
        # given
        source = _Bytes(b"x" * 100)
        source.changed = True
        # when
        error = await self.failure("read-all", source)
        # then
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)
        self.assertIn("changed", error.reason)

    async def test_no_tracks_is_invalid(self) -> None:
        self.assertEqual((await self.failure("no-tracks")).kind, ProbeFailure.INVALID)

    async def test_null_media_is_invalid(self) -> None:
        self.assertEqual((await self.failure("null")).kind, ProbeFailure.INVALID)

    async def test_zero_dimension_is_invalid(self) -> None:
        self.assertEqual((await self.failure("zero")).kind, ProbeFailure.INVALID)

    async def test_reported_error_is_transient_and_hides_the_token(self) -> None:
        # given
        runner = await self.runner("error")
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await runner.probe(_Bytes(b"x"), is_image=False)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)
        self.assertIn("404", cm.exception.reason)
        self.assertNotIn("http://", cm.exception.reason)

    async def test_malformed_output_is_transient(self) -> None:
        self.assertEqual((await self.failure("garbage")).kind, ProbeFailure.TRANSIENT)

    async def test_nonzero_exit_is_transient(self) -> None:
        error = await self.failure("exit")
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)
        self.assertIn("exit 3", error.reason)

    async def test_crash_is_transient(self) -> None:
        error = await self.failure("crash")
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)
        self.assertIn("signal", error.reason)

    async def test_stderr_flood_does_not_block(self) -> None:
        # given
        runner = await self.runner("flood")
        # when / then
        self.assertEqual(await runner.probe(_Bytes(b"x"), is_image=False), (7, 5))

    async def test_deadline_while_parsing_is_a_limit(self) -> None:
        # when
        error = await self.failure(
            "hang", limits=ProbeLimits(probe_timeout=0.5, read_timeout=5)
        )
        # then
        self.assertEqual(error.kind, ProbeFailure.LIMIT)

    async def test_deadline_while_waiting_for_the_source_is_transient(self) -> None:
        # given
        source = _Stalled(b"x" * 100)
        # when
        error = await self.failure(
            "hang-reading",
            source,
            limits=ProbeLimits(probe_timeout=1, read_timeout=30),
        )
        # then
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)

    async def test_stalled_read_is_transient(self) -> None:
        # when
        with self.assertLogs("aiohttp.server", "ERROR"):
            error = await self.failure(
                "hang-reading",
                _Stalled(b"x" * 100),
                limits=ProbeLimits(probe_timeout=10, read_timeout=0.3),
            )
        # then
        self.assertEqual(error.kind, ProbeFailure.TRANSIENT)

    async def test_cancellation_kills_the_subprocess(self) -> None:
        # given
        runner = await self.runner("hang")
        task = asyncio.ensure_future(runner.probe(_Bytes(b"x"), is_image=False))
        await asyncio.sleep(0.5)
        # when
        task.cancel()
        # then
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(runner._running, set())  # pyright: ignore[reportPrivateUsage]

    async def test_close_stops_a_running_probe(self) -> None:
        # given
        runner = await self.runner("hang")
        task = asyncio.ensure_future(runner.probe(_Bytes(b"x"), is_image=False))
        await asyncio.sleep(0.5)
        # when
        await runner.close()
        # then
        with self.assertRaises(MediaProbeError) as cm:
            await task
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)
        with self.assertRaises(ProbeUnavailableError):
            await runner.probe(_Bytes(b"x"), is_image=False)

    async def test_local_descriptor_is_read_directly(self) -> None:
        # given
        runner = await self.runner("read-fd")
        path = self.script.parent / "a.bin"
        path.write_bytes(b"x" * 1234)
        source = _LocalFile(path)
        self.addCleanup(source.file.close)
        source.file.seek(100)
        # when
        dims = await runner.probe(source, is_image=False)
        # then: the whole file, through /dev/fd, and nothing over HTTP
        self.assertEqual(dims, (1234, 1))
        self.assertEqual(source.streams, [])
        self.assertEqual(source.file.tell(), 100)

    async def test_local_descriptor_still_checks_the_source(self) -> None:
        # given
        runner = await self.runner("read-fd")
        path = self.script.parent / "a.bin"
        path.write_bytes(b"x" * 10)
        source = _LocalFile(path)
        self.addCleanup(source.file.close)
        source.changed = True
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await runner.probe(source, is_image=False)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)

    async def test_local_descriptor_deadline_is_a_limit(self) -> None:
        # given
        runner = await self.runner(
            "hang", limits=ProbeLimits(probe_timeout=0.5, read_timeout=5)
        )
        path = self.script.parent / "a.bin"
        path.write_bytes(b"x")
        source = _LocalFile(path)
        self.addCleanup(source.file.close)
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await runner.probe(source, is_image=False)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.LIMIT)

    async def test_failed_recheck_names_only_the_exception_type(self) -> None:
        # given: a recheck failing with an error that carries a session ID
        class _Unreachable(_Bytes):
            async def unchanged(self) -> bool:
                raise OSError("GET ?_sid=secret")

        runner = await self.runner("read-all")
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await runner.probe(_Unreachable(b"x" * 10), is_image=False)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)
        self.assertIn("OSError", cm.exception.reason)
        self.assertNotIn("secret", cm.exception.reason)
        self.assertIsNone(cm.exception.__cause__)

    async def test_close_stops_a_hung_check(self) -> None:
        # given: the first probe is checking MediaInfo, which hangs
        runner = await self.runner("hang-version", checked=False)
        probe = asyncio.ensure_future(runner.probe(_Bytes(b"x"), is_image=False))
        await asyncio.sleep(0.5)
        checked = runner._checked  # pyright: ignore[reportPrivateUsage]
        assert checked is not None
        # when
        async with asyncio.timeout(5):
            await runner.close()
        # then
        self.assertTrue(checked.cancelled())
        await asyncio.gather(probe, return_exceptions=True)

    async def test_proxy_variables_are_not_passed_on(self) -> None:
        # given: a proxy that would receive the probe's loopback URL
        proxies = {
            "http_proxy": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "socks5://127.0.0.1:9",
            "no_proxy": "example.com",
        }
        runner = await self.runner("env")
        # when
        with patch.dict(os.environ, proxies):
            dims = await runner.probe(_Bytes(b"x"), is_image=False)
        # then
        self.assertEqual(dims, (1, 1))

    async def test_check_rejects_an_old_version(self) -> None:
        # given
        runner = await self.runner("version=24.12", checked=False)
        # when / then
        with self.assertRaises(ProbeUnavailableError) as cm:
            await runner.check()
        self.assertIn("24.12", str(cm.exception))

    async def test_check_rejects_a_mediainfo_that_cannot_probe(self) -> None:
        # given
        runner = await self.runner("null", checked=False)
        # when / then
        with self.assertRaises(ProbeUnavailableError):
            await runner.check()

    async def test_probe_runs_the_check_first(self) -> None:
        # given: offline commands probe without an explicit check
        runner = await self.runner("version=24.12", checked=False)
        # when / then
        with self.assertRaises(ProbeUnavailableError):
            await runner.probe(_Bytes(b"x"), is_image=False)

    async def test_shutdown_kills_a_probe_spawned_meanwhile(self) -> None:
        # given: close() runs while the subprocess is being created
        runner = await self.runner("hang")
        spawn = asyncio.create_subprocess_exec
        closing = asyncio.Event()

        async def slow_spawn(*args: object, **kwargs: object) -> object:
            await closing.wait()
            return await spawn(*args, **kwargs)  # type: ignore[arg-type]

        with patch("asyncio.create_subprocess_exec", slow_spawn):
            task = asyncio.ensure_future(runner.probe(_Bytes(b"x"), is_image=False))
            await asyncio.sleep(0.1)
            # when
            await runner.close()
            closing.set()
            # then
            with self.assertRaises(MediaProbeError):
                async with asyncio.timeout(5):
                    await task

    async def test_missing_executable_is_unavailable(self) -> None:
        # given
        runner = await self.enterAsyncContext(
            ProbeRunner(command=[str(self.script.parent / "missing")])
        )
        # when / then
        with self.assertRaises(ProbeUnavailableError):
            await runner.check()
        with self.assertRaises(ProbeUnavailableError):
            await runner.probe(_Bytes(b"x"), is_image=False)


@requires_mediainfo
class TestRealMediaInfo(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.probes = await self.enterAsyncContext(
            ProbeRunner(
                workers=1, limits=ProbeLimits(probe_timeout=20, read_timeout=10)
            )
        )

    async def test_probes_with_a_proxy_set(self) -> None:
        # given: libcurl would send the loopback request to this dead proxy
        proxies = {
            "http_proxy": "http://127.0.0.1:9",
            "ALL_PROXY": "socks5://127.0.0.1:9",
        }
        # when
        with patch.dict(os.environ, proxies):
            dims = await self.probes.probe(_Bytes(_png(7, 5)), is_image=True)
        # then
        self.assertEqual(dims, (7, 5))

    async def test_check_passes(self) -> None:
        await self.probes.check()

    async def test_image_dimensions(self) -> None:
        # when / then
        self.assertEqual(
            await self.probes.probe(_Bytes(_png(7, 5)), is_image=True), (7, 5)
        )

    async def test_video_with_moov_at_the_end(self) -> None:
        # given
        source = _Bytes((_MEDIA / "moov_at_end.mp4").read_bytes())
        # when
        dims = await self.probes.probe(source, is_image=False)
        # then
        self.assertEqual(dims, (32, 24))

    async def test_unparsable_bytes_are_invalid(self) -> None:
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await self.probes.probe(_Bytes(b"not media" * 100), is_image=True)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.INVALID)

    async def test_empty_file_is_invalid(self) -> None:
        # when
        with self.assertRaises(MediaProbeError) as cm:
            await self.probes.probe(_Bytes(b""), is_image=True)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.INVALID)

    async def test_interrupted_transfer_is_transient(self) -> None:
        # given: MediaInfo would report the dimensions of this partial read
        source = _Failing((_MEDIA / "moov_at_end.mp4").read_bytes())
        # when
        with (
            self.assertLogs("aiohttp.server", "ERROR"),
            self.assertRaises(MediaProbeError) as cm,
        ):
            await self.probes.probe(source, is_image=False)
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)
