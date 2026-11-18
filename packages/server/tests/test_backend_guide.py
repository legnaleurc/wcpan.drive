"""The complete example in ``docs/writing-a-backend.md`` really works."""

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import IsolatedAsyncioTestCase

from wcpan.drive.server.exceptions import (
    OperationConflictError,
    UploadConflictError,
)
from wcpan.drive.server.testing import (
    BackendLifecycle,
    backend_harness,
    enter_backend,
    mount_id,
)


_GUIDE = Path(__file__).parent.parent / "docs" / "writing-a-backend.md"
_MODULE = "guide_memory_backend"
_DOCS = mount_id("docs")


def _load_example() -> Any:
    section = _GUIDE.read_text().split("## Complete example", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    assert match is not None
    module = ModuleType(_MODULE)
    sys.modules[_MODULE] = module
    exec(compile(match[1], str(_GUIDE), "exec"), module.__dict__)  # noqa: S102
    return module


async def _content(data: bytes) -> AsyncIterator[bytes]:
    yield data


class GuideExampleTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.addCleanup(sys.modules.pop, _MODULE, None)
        self.example = _load_example()
        self.source = self.example.SOURCES.setdefault("scratch", self.example.Source())
        folder = self.source.add("docs", "folder", folder=True)
        self.nested = self.source.add(folder.key, "nested.txt", data=b"hello")
        self.server = await self.enterAsyncContext(backend_harness())
        self.mirror = self.server.mirror
        self.instance = await self.server.add_instance(
            "scratch", self.example.SCHEMA, {"docs": "docs"}
        )
        self.backend = await self.enterAsyncContext(
            enter_backend(self.example.create_backend, self.instance.context, {})
        )
        lifecycle = BackendLifecycle(self.backend, self.instance.core)
        self.running = asyncio.create_task(lifecycle.run())
        self.addCleanup(self.running.cancel)
        async with asyncio.timeout(5):
            await lifecycle.until_ready()
        self.core = self.instance.core

    async def child(self, parent: str, name: str) -> Any:
        children = await self.mirror.get_children(parent)
        return next((c for c in children if c.name == name), None)

    async def settled(self) -> None:
        async with asyncio.timeout(5):
            await self.core.hints.join()
            await self.core.enrichment.drain()

    async def test_startup_scan_mirrors_the_source(self) -> None:
        # when
        folder = await self.child(_DOCS, "folder")
        # then
        self.assertIsNotNone(await self.child(folder.id, "nested.txt"))

    async def test_notified_changes_reach_the_mirror(self) -> None:
        # given
        self.source.add("docs", "new.txt", data=b"x")
        self.source.remove(self.nested.key)
        # when
        self.backend.notify("changed")
        await self.settled()
        # then
        folder = await self.child(_DOCS, "folder")
        self.assertIsNotNone(await self.child(_DOCS, "new.txt"))
        self.assertIsNone(await self.child(folder.id, "nested.txt"))

    async def test_folder_operations(self) -> None:
        # given
        operations = self.backend.operations
        created = await operations.create_folder(_DOCS, "made")
        with self.assertRaises(OperationConflictError):
            await operations.create_folder(_DOCS, "made")
        # when
        renamed = await operations.update(created, name="renamed", parent_id=None)
        await self.core.writes_landed()
        # then
        self.assertEqual(renamed.id, created.id)
        self.assertIsNotNone(await self.child(_DOCS, "renamed"))
        # when
        await operations.delete(renamed)
        await self.core.writes_landed()
        # then
        self.assertIsNone(await self.child(_DOCS, "renamed"))

    async def test_upload_and_download(self) -> None:
        # given
        uploader = self.backend.uploader
        record = await uploader.upload(
            _DOCS, "up.bin", _content(b"abcdef"), mime_type=None, media_info=None
        )
        await self.core.writes_landed()
        # when
        operations = self.backend.operations
        async with operations.download(record, offset=1, length=3) as stream:
            data = b"".join([chunk async for chunk in stream])
        # then
        self.assertEqual(data, b"bcd")
        self.assertIsNotNone(await uploader.find_conflict(_DOCS, "up.bin"))
        with self.assertRaises(UploadConflictError):
            await uploader.upload(
                _DOCS, "up.bin", _content(b"x"), mime_type=None, media_info=None
            )
