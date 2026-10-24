"""Tests for change-scan record conversion."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.server.services.enricher import (
    MediaEnrichmentError,
    MediaEnrichService,
)
from wcpan.drive.server.services.off_main import OffMainService
from wcpan.drive.server.services.paths import LocalPathService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.synology.convert import (
    SynologyLocator,
    convert_file_info,
)
from wcpan.drive.server.types import NodeObservation
from wcpan.drive.server.workers import (
    create_metadata_queue,
    create_write_queue,
)


def _convert(info, parent_id):
    observation = convert_file_info(info, parent_id)
    assert observation is not None
    return observation.record


class TestConvertSynologyFileInfo(TestCase):
    def test_directory_record(self):
        # given
        info = {
            "file_id": "d1",
            "permanent_link": "d1",
            "parent_id": "p",
            "name": "folder",
            "type": "dir",
            "content_type": "dir",
            "size": 0,
            "created_time": 1700000000,
            "modified_time": 1700003600,
            "change_time": 0,
            "sync_id": 5,
        }
        parent_id = "_mount"
        # when
        observation = convert_file_info(info, parent_id)
        assert observation is not None
        record = observation.record
        # then
        self.assertEqual(observation.locator, "d1")
        self.assertTrue(record.is_directory)
        self.assertEqual(record.mime_type, FOLDER_MIME_TYPE)
        self.assertEqual(record.id, "d1")
        self.assertEqual(record.parent_id, parent_id)
        self.assertEqual(record.created_time, 1700000000)
        self.assertEqual(record.modified_time, 1700003600)

    def test_file_with_image_content_type(self):
        # given
        info = {
            "file_id": "f1",
            "permanent_link": "f1",
            "parent_id": "p",
            "name": "pic.png",
            "type": "file",
            "content_type": "image",
            "hash": "abc",
            "size": 1024,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
        }
        # when
        record = _convert(info, parent_id="par")
        # then
        self.assertFalse(record.is_directory)
        self.assertTrue(record.is_image)
        self.assertFalse(record.is_video)
        self.assertEqual(record.hash, "abc")
        self.assertEqual(record.hash_algorithm, "md4")
        self.assertEqual(record.size, 1024)

    def test_file_with_video_content_type(self):
        # given
        info = {
            "file_id": "v1",
            "permanent_link": "v1",
            "parent_id": "p",
            "name": "clip.mp4",
            "type": "file",
            "content_type": "video",
            "size": 999,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 2,
        }
        # when
        record = _convert(info, parent_id=None)
        # then
        self.assertTrue(record.is_video)
        self.assertIsNone(record.parent_id)

    def test_file_with_image_metadata_does_not_fill_dimensions(self):
        info = {
            "file_id": "f2",
            "permanent_link": "f2",
            "parent_id": "p",
            "name": "wide.png",
            "type": "file",
            "content_type": "image",
            "size": 10,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
            "image_metadata": {"width": 1920, "height": 1080},
        }
        record = _convert(info, parent_id="par")
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)
        self.assertEqual(record.ms_duration, 0)

    def test_file_with_nested_image_metadata_does_not_fill_dimensions(self):
        info = {
            "file_id": "f3",
            "permanent_link": "f3",
            "parent_id": "p",
            "name": "nested.jpg",
            "type": "file",
            "content_type": "image",
            "size": 10,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 1,
            "image_metadata": {"resolution": {"width": 640, "height": 480}},
        }
        record = _convert(info, parent_id="par")
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)

    def test_video_with_image_metadata_does_not_fill_duration(self):
        info = {
            "file_id": "v2",
            "permanent_link": "v2",
            "parent_id": "p",
            "name": "clip.mp4",
            "type": "file",
            "content_type": "video",
            "size": 100,
            "created_time": 0,
            "modified_time": 0,
            "change_time": 0,
            "sync_id": 2,
            "image_metadata": {
                "width": 1280,
                "height": 720,
                "duration": 5000,
            },
        }
        record = _convert(info, parent_id="root")
        self.assertEqual(record.width, 0)
        self.assertEqual(record.height, 0)
        self.assertEqual(record.ms_duration, 0)


class TestEnrichPreservesApiDimensions(IsolatedAsyncioTestCase):
    async def test_enrich_without_force_refresh_does_not_probe_when_api_filled(self):
        record = _convert(
            {
                "file_id": "f4",
                "permanent_link": "f4",
                "parent_id": "p",
                "name": "api.png",
                "type": "file",
                "content_type": "image",
                "size": 10,
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 1,
            },
            parent_id="par",
        )
        record = replace(record, width=800, height=600)
        local_path_svc = LocalPathService(
            storage=MagicMock(),
            mounts={},
        )
        enricher = MediaEnrichService(local_path_service=local_path_svc)
        with patch(
            "wcpan.drive.server.services.enricher._probe_sync",
            side_effect=AssertionError(
                "probe must not run when API supplied dimensions"
            ),
        ):
            out = await enricher.enrich(record, force_refresh=False)
        self.assertEqual(out.width, 800)
        self.assertEqual(out.height, 600)

    async def test_enrich_force_refresh_invokes_probe_when_api_filled(self) -> None:
        record = _convert(
            {
                "file_id": "f5",
                "permanent_link": "f5",
                "parent_id": "p",
                "name": "api2.png",
                "type": "file",
                "content_type": "image",
                "size": 10,
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 1,
            },
            parent_id="par",
        )
        record = replace(record, width=800, height=600)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            probe_path = Path(tmp.name)
        try:
            with ThreadPoolExecutor(1) as pool:
                off_main = OffMainService(pool=pool)
                local_path_svc = LocalPathService(
                    storage=MagicMock(),
                    mounts={},
                )
                enricher = MediaEnrichService(
                    local_path_service=local_path_svc,
                    off_main=off_main,
                )
                with (
                    patch.object(
                        local_path_svc,
                        "resolve_local_path",
                        new=AsyncMock(return_value=probe_path),
                    ),
                    patch(
                        "wcpan.drive.server.services.enricher._probe_sync",
                        return_value=(100, 200, 0),
                    ) as probe,
                ):
                    out = await enricher.enrich(record, force_refresh=True)
            probe.assert_called_once()
            self.assertEqual(out.width, 100)
            self.assertEqual(out.height, 200)
        finally:
            probe_path.unlink(missing_ok=True)

    async def test_enrich_raises_when_required_probe_fails(self) -> None:
        record = _convert(
            {
                "file_id": "f6",
                "permanent_link": "f6",
                "parent_id": "p",
                "name": "broken.png",
                "type": "file",
                "content_type": "image",
                "size": 10,
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 1,
            },
            parent_id="par",
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            probe_path = Path(tmp.name)
        try:
            with ThreadPoolExecutor(1) as pool:
                off_main = OffMainService(pool=pool)
                local_path_svc = LocalPathService(
                    storage=MagicMock(),
                    mounts={},
                )
                enricher = MediaEnrichService(
                    local_path_service=local_path_svc,
                    off_main=off_main,
                )
                with (
                    patch.object(
                        local_path_svc,
                        "resolve_local_path",
                        new=AsyncMock(return_value=probe_path),
                    ),
                    patch(
                        "wcpan.drive.server.services.enricher._probe_sync",
                        side_effect=ValueError("bad media"),
                    ),
                ):
                    with self.assertRaises(MediaEnrichmentError):
                        await enricher.enrich(record, force_refresh=True)
        finally:
            probe_path.unlink(missing_ok=True)


class TestNodeSyncServiceMetadataQueue(IsolatedAsyncioTestCase):
    async def test_process_metadata_reconcile_insert_enqueues_write(self) -> None:
        wq = create_write_queue()
        mq = create_metadata_queue()
        with ThreadPoolExecutor(1) as pool:
            off_main = OffMainService(pool=pool)
            storage = MagicMock()
            cs = NodeSyncService(
                storage=storage,
                write_queue=wq,
                off_main=off_main,
                mounts={},
                metadata_queue=mq,
            )
            info = {
                "file_id": "n1",
                "permanent_link": "n1",
                "parent_id": "p",
                "name": "a.png",
                "type": "file",
                "content_type": "image",
                "size": 1,
                "created_time": 0,
                "modified_time": 0,
                "change_time": 0,
                "sync_id": 1,
            }
            observation = convert_file_info(info, parent_id="par")
            assert observation is not None
            record = observation.record
            storage.apply_observations = AsyncMock(return_value={record.id})
            async with cs.observe() as read:
                await cs.reconcile_insert(observation, read=read)
            item = await mq.get()
            mq.task_done()
            with patch.object(
                cs._enricher,
                "enrich",
                new=AsyncMock(return_value=record),
            ):
                await cs.process_metadata_item(item)
            job = await wq.get()
            try:
                await job()
            finally:
                wq.task_done()
            storage.apply_observations.assert_awaited_once_with(
                [NodeObservation(record=record, locator=SynologyLocator("n1"))]
            )
