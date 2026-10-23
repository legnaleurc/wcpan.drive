"""MediaEnrichService: when files are probed, and how failures surface."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server._services.media.enricher import (
    MediaEnrichmentError,
    MediaEnrichService,
)
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probe.source import (
    MediaProbeError,
    MediaSource,
    ProbeFailure,
    ProbeUnavailableError,
)
from wcpan.drive.server._services.media.probing import (
    MediaProbing,
    ProbesNotDeclaredError,
)
from wcpan.drive.server._types import NodeObservation


def _image(width: int = 0, height: int = 0) -> NodeObservation:
    t = 1_704_067_200
    record = NodeRecord(
        id=NodeId("img"),
        parent_id=NodeId("parent"),
        name="a.jpg",
        is_directory=False,
        created_time=t,
        modified_time=t,
        changed_time=t,
        mime_type="image/jpeg",
        hash="",
        hash_algorithm="",
        size=4,
        is_image=True,
        is_video=False,
        width=width,
        height=height,
    )
    return NodeObservation(record=record, locator=None, backend="b", source_key="k")


class TestEnrich(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.probing = MediaProbing(runner=ProbeRunner())
        self.probe = AsyncMock(return_value=(640, 480))
        self.probing.probe = self.probe
        self.service = MediaEnrichService(probing=self.probing)

    async def test_probed_dimensions_are_applied(self) -> None:
        # when
        record = await self.service.enrich(_image())
        # then
        self.assertEqual((record.width, record.height), (640, 480))

    async def test_complete_dimensions_are_kept_without_probing(self) -> None:
        # when
        record = await self.service.enrich(_image(800, 600))
        # then
        self.assertEqual((record.width, record.height), (800, 600))
        self.probe.assert_not_awaited()

    async def test_force_refresh_probes_complete_dimensions(self) -> None:
        # when
        record = await self.service.enrich(_image(800, 600), force_refresh=True)
        # then
        self.assertEqual((record.width, record.height), (640, 480))

    async def test_probe_failure_keeps_its_kind(self) -> None:
        # given
        self.probe.side_effect = MediaProbeError(ProbeFailure.LIMIT, "too big")
        # when
        with self.assertRaises(MediaEnrichmentError) as cm:
            await self.service.enrich(_image())
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.LIMIT)


class TestMediaProbing(IsolatedAsyncioTestCase):
    async def test_backend_without_a_source_fails_transiently(self) -> None:
        # given
        service = MediaEnrichService(probing=MediaProbing(runner=ProbeRunner()))
        # when
        with self.assertRaises(MediaEnrichmentError) as cm:
            await service.enrich(_image())
        # then
        self.assertEqual(cm.exception.kind, ProbeFailure.TRANSIENT)


class TestProbingNotDeclared(IsolatedAsyncioTestCase):
    """Without a runner, sources register but a needed probe stops the command."""

    def setUp(self) -> None:
        self.probing = MediaProbing(runner=None)
        self.service = MediaEnrichService(probing=self.probing)

    async def test_a_file_that_needs_probing_raises(self) -> None:
        # given
        @asynccontextmanager
        async def open_source(_: NodeObservation) -> AsyncGenerator[MediaSource]:
            raise AssertionError("must not open the file")
            yield

        with self.probing.register("b", open_source):
            # when / then
            with self.assertRaises(ProbesNotDeclaredError):
                await self.service.enrich(_image())
        # then: it stops the command, as MediaInfo being unavailable does
        self.assertTrue(issubclass(ProbesNotDeclaredError, ProbeUnavailableError))

    async def test_files_that_need_no_probe_pass(self) -> None:
        # when
        record = await self.service.enrich(_image(800, 600))
        # then
        self.assertEqual((record.width, record.height), (800, 600))

    async def test_check_raises(self) -> None:
        # when / then
        with self.assertRaises(ProbesNotDeclaredError):
            await self.probing.check()
