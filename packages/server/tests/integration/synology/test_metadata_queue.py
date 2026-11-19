"""Queued enrichment of Synology observations."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from decoy import matchers
from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE
from wcpan.drive.server._lib.write_queue import (
    create_write_queue,
)
from wcpan.drive.server._services.media.enricher import (
    MediaEnrichmentError,
    MediaEnrichService,
)
from wcpan.drive.server._services.media.probe.runner import ProbeRunner
from wcpan.drive.server._services.media.probing import MediaProbing
from wcpan.drive.server._services.mirror.store import (
    MirrorStore,
)
from wcpan.drive.server._services.off_main import OffMainService
from wcpan.drive.server._services.sync.metadata import (
    create_metadata_queue,
)
from wcpan.drive.server._services.sync.service import NodeSyncService
from wcpan.drive.server._types import NodeObservation
from wcpan.drive.server.backend.synology._convert import (
    SynologyLocator,
    convert_file_info,
)

from ...backend.synology._lib import DecoyTestCase


def _convert(info, parent_id):
    observation = convert_file_info(info, parent_id)
    assert observation is not None
    return observation.record


class TestNodeSyncServiceMetadataQueue(DecoyTestCase):
    async def test_process_metadata_of_a_new_file_enqueues_write(self) -> None:
        # given
        wq = create_write_queue()
        mq = create_metadata_queue()
        with ThreadPoolExecutor(1) as pool:
            off_main = OffMainService(pool=pool)
            mirror = self.decoy.mock(cls=MirrorStore)
            cs = NodeSyncService(
                mirror=mirror,
                write_queue=wq,
                probing=MediaProbing(runner=ProbeRunner()),
                metadata_queue=mq,
            )
            # No write worker runs; the test takes the queued write by hand.
            setattr(cs, "barrier", AsyncMock())
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
            self.decoy.when(await mirror.get_nodes_by_ids([record.id])).then_return({})
            applied = matchers.Captor()
            self.decoy.when(await mirror.apply_observations(applied)).then_return(
                {record.id}
            )
            # when
            async with cs.observe() as read:
                await cs.upsert_file_batch([observation], read=read)
            item = await mq.get()
            mq.task_done()
            with patch.object(
                cs._prober._enricher,
                "enrich",
                new=AsyncMock(return_value=record),
            ):
                await cs.process_metadata_item(item)
            job = await wq.get()
            try:
                await job()
            finally:
                wq.task_done()
            # then
            self.assertEqual(
                applied.values,
                [
                    [
                        NodeObservation(
                            record=record,
                            locator=SynologyLocator("n1"),
                            source_key="n1",
                        )
                    ]
                ],
            )
