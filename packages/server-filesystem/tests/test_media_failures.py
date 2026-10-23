"""Files whose media probe fails: withheld without an ID, keyed by path."""

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator

from wcpan.drive.protocol.types import MediaInfo
from wcpan.drive.server._services.media.probe.source import ProbeFailure
from wcpan.drive.server._services.media.retries import MediaRetries
from wcpan.drive.server._services.upload.service import (
    UploadInvalidMediaError,
    UploadTransientError,
)

from ._harness import DOCS, ScanTestCase


async def _content(data: bytes) -> AsyncIterator[bytes]:
    yield data


class TestMediaFailures(ScanTestCase):
    """Probes read the files through the backend; content starting with
    ``bad`` is invalid media."""

    async def failures(self) -> list[str]:
        return [f.source_key for f in await self.mirror.get_media_failures("fs")]

    def mapped_ids(self) -> int:
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute("SELECT COUNT(*) FROM node_ids").fetchone()[0]
        finally:
            con.close()

    async def test_withheld_file_keeps_no_id_across_rescans(self) -> None:
        # given
        path = self.write("bad.jpg", b"bad jpeg")
        self.write("good.txt", b"text")

        # when
        await self.scan()
        ids_after_first = self.mapped_ids()
        await self.scan()

        # then: one record by path; only the published file has an ID
        self.assertIsNone(await self.node("bad.jpg"))
        self.assertEqual(await self.failures(), [str(path)])
        self.assertEqual(self.mapped_ids(), ids_after_first)
        self.assertEqual(self.mapped_ids(), 1)

    async def test_repaired_file_is_published_and_its_record_cleared(self) -> None:
        # given
        self.write("bad.jpg", b"bad jpeg")
        await self.scan()

        # when: the same file becomes readable
        self.probes.invalid_prefix = None
        await self.scan()

        # then
        repaired = await self.node("bad.jpg")
        assert repaired is not None
        self.assertEqual((repaired.width, repaired.height), (4, 3))
        self.assertEqual(await self.failures(), [])

    async def test_rescan_prunes_records_it_did_not_renew(self) -> None:
        # given
        path = self.write("bad.jpg", b"bad jpeg")
        await self.scan()
        path.unlink()
        started = time.time_ns() // 1_000_000

        # when: what a full scan does
        await self.scan()
        removed = await self.node_sync.prune_media_failures(before=started)

        # then
        self.assertEqual(removed, 1)
        self.assertEqual(await self.failures(), [])

    async def test_rescan_keeps_records_it_renewed(self) -> None:
        # given
        path = self.write("bad.jpg", b"bad jpeg")
        await self.scan()
        started = time.time_ns() // 1_000_000

        # when
        await self.scan()
        await self.node_sync.prune_media_failures(before=started)

        # then
        self.assertEqual(await self.failures(), [str(path)])

    async def test_failure_records_its_kind(self) -> None:
        # given
        self.write("bad.jpg", b"bad jpeg")
        # when
        await self.scan()
        # then
        (failure,) = await self.mirror.get_media_failures("fs")
        self.assertEqual(failure.kind, ProbeFailure.INVALID)

    async def test_transient_failure_is_retried_by_reading_its_folder(self) -> None:
        # given: the first probe fails transiently
        self.node_sync._retries = MediaRetries(first_delay=0.01)
        self.probes.transient = 1
        path = self.write("sub/pic.jpg", b"jpeg")
        async with self.node_sync.retrying_with(self.scanner.rescan_parent):
            # when
            await self.scan()
            (failure,) = await self.mirror.get_media_failures("fs")
            self.assertEqual(
                (failure.source_key, failure.kind),
                (str(path), ProbeFailure.TRANSIENT),
            )
            async with asyncio.timeout(5):
                while await self.node("sub/pic.jpg") is None:
                    await asyncio.sleep(0.01)

        # then
        published = await self.node("sub/pic.jpg")
        assert published is not None
        self.assertEqual((published.width, published.height), (4, 3))
        self.assertEqual(await self.failures(), [])

    async def test_pruning_forgets_the_retry_of_a_removed_file(self) -> None:
        # given: a transient failure with a retry pending, then the file goes
        async with self.node_sync.retrying_with(self.scanner.rescan_parent):
            self.probes.transient = 1
            path = self.write("pic.jpg", b"jpeg")
            await self.scan()
            self.assertEqual(self.node_sync._retries.pending("fs"), {str(path)})
            path.unlink()
            started = time.time_ns() // 1_000_000

            # when: what a full scan does
            await self.scan()
            await self.node_sync.prune_media_failures(before=started)

            # then
            self.assertEqual(await self.failures(), [])
            self.assertEqual(self.node_sync._retries.pending("fs"), set())

    async def test_unreadable_upload_is_rejected_unpublished(self) -> None:
        # when / then
        with self.assertRaises(UploadInvalidMediaError):
            await self.uploader.upload(
                DOCS,
                "bad.jpg",
                _content(b"bad jpeg"),
                mime_type=None,
                media_info=None,
            )
        await self.node_sync.wait_enrichment_drained()
        self.assertFalse((self.source / "bad.jpg").exists())
        self.assertIsNone(await self.node("bad.jpg"))
        self.assertEqual(await self.failures(), [])

    async def test_upload_is_probed_before_it_is_published(self) -> None:
        # when
        record = await self.uploader.upload(
            DOCS, "pic.jpg", _content(b"jpeg"), mime_type=None, media_info=None
        )
        # then
        self.assertEqual((record.width, record.height), (4, 3))
        self.assertEqual(self.probes.probed, [b"jpeg"])

    async def test_transient_probe_failure_keeps_the_upload_for_a_retry(
        self,
    ) -> None:
        # given
        self.probes.transient = 1
        file = await self.uploader.open(
            DOCS, "pic.jpg", 4, mime_type=None, media_info=None
        )
        self.addAsyncCleanup(file.abort)
        await file.write(0, b"jpeg")
        # when
        with self.assertRaises(UploadTransientError):
            await file.close()
        # then
        self.assertFalse((self.source / "pic.jpg").exists())
        record = await file.close()
        self.assertEqual((record.width, record.height), (4, 3))
        self.assertTrue((self.source / "pic.jpg").exists())

    async def test_client_media_info_is_trusted_without_probing(self) -> None:
        # when
        record = await self.uploader.upload(
            DOCS,
            "bad.jpg",
            _content(b"bad jpeg"),
            mime_type=None,
            media_info=MediaInfo(is_image=True, is_video=False, width=4, height=3),
        )

        # then
        self.assertEqual((record.width, record.height), (4, 3))
        self.assertTrue((self.source / "bad.jpg").exists())
