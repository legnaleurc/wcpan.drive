"""Media probes read files where they are, bound to the observed state."""

import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, skipUnless

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import (
    MediaProbeError,
    MediaSourceError,
    ProbeFailure,
    ProbeUnavailableError,
    read_exactly,
)
from wcpan.drive.server._types import NodeObservation
from wcpan.drive.server.filesystem._blocking import Blocking
from wcpan.drive.server.filesystem._media import media_sources
from wcpan.drive.server.filesystem._reading import stat_entry
from wcpan.drive.server.filesystem._scan import FilesystemScanner


class TestFileMediaSource(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "a.jpg"
        self.path.write_bytes(b"0123456789")
        pool = ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown)
        self.open_source = media_sources(Blocking(executor=pool))

    def observe(self) -> NodeObservation:
        entry = stat_entry(str(self.path))
        assert entry is not None
        return FilesystemScanner.observation(entry, NodeId("a"), NodeId("p"), "")

    async def test_reads_exact_ranges(self) -> None:
        # given
        observation = self.observe()
        # when
        async with self.open_source(observation) as source:
            # then
            self.assertEqual(source.size, 10)
            self.assertEqual(await read_exactly(source, 3, 4), b"3456")
            self.assertTrue(await source.unchanged())

    async def test_truncated_file_fails_the_stream(self) -> None:
        # given
        observation = self.observe()
        async with self.open_source(observation) as source:
            os.truncate(self.path, 5)
            # when / then
            with self.assertRaises(MediaSourceError):
                await read_exactly(source, 3, 4)

    async def test_change_during_the_probe_is_detected(self) -> None:
        # given
        observation = self.observe()
        async with self.open_source(observation) as source:
            await read_exactly(source, 0, 10)
            # when
            with open(self.path, "ab") as f:
                f.write(b"more")
            # then
            self.assertFalse(await source.unchanged())

    async def test_change_since_the_observation_fails_to_open(self) -> None:
        # given
        observation = self.observe()
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        # when / then
        with self.assertRaises(MediaSourceError):
            async with self.open_source(observation):
                pass

    async def test_replaced_file_fails_to_open(self) -> None:
        # given
        observation = self.observe()
        self.path.unlink()
        self.path.write_bytes(b"0123456789")
        # when / then
        with self.assertRaises(FileNotFoundError):
            async with self.open_source(observation):
                pass


def _png(width: int, height: int) -> bytes:
    import struct
    import zlib

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


@skipUnless(sys.platform == "linux", "the /dev/fd shortcut is Linux only")
@skipUnless(shutil.which("mediainfo"), "needs mediainfo")
class TestDescriptorProbe(IsolatedAsyncioTestCase):
    """MediaInfo reads the verified file through /dev/fd, not its path."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "a.png"
        self.path.write_bytes(_png(7, 5))
        pool = ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown)
        self.open_source = media_sources(Blocking(executor=pool))
        self.runner = await self.enterAsyncContext(ProbeRunner(workers=1))
        try:
            await self.runner.check()
        except ProbeUnavailableError as e:
            self.skipTest(str(e))

    async def test_reads_the_opened_file_after_a_rename_over_its_path(self) -> None:
        # given
        entry = stat_entry(str(self.path))
        assert entry is not None
        observation = FilesystemScanner.observation(entry, NodeId("a"), NodeId("p"), "")
        async with self.open_source(observation) as source:
            fd = getattr(source, "local_fd")()
            self.assertIsNotNone(fd)
            other = self.path.with_name("b.png")
            other.write_bytes(_png(3, 2))
            # when
            os.replace(other, self.path)
            # then: MediaInfo reads the verified inode, not the new path
            dims = await self.runner._probe_local(fd)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(dims, (7, 5))
            # and the unlink changed its ctime, so the probe is discarded
            with self.assertRaises(MediaProbeError) as cm:
                await self.runner.probe(source, is_image=True)
            self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)

    async def test_probes_an_unchanged_file(self) -> None:
        # given
        entry = stat_entry(str(self.path))
        assert entry is not None
        observation = FilesystemScanner.observation(entry, NodeId("a"), NodeId("p"), "")
        async with self.open_source(observation) as source:
            # when / then
            self.assertEqual(await self.runner.probe(source, is_image=True), (7, 5))
