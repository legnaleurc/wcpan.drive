"""The CLI verifies downloads from one feed server with mixed hash algorithms.

A server can mount backends that report different algorithms (Synology ``md4``,
filesystem ``md5``), so one listing mixes them. Each file must be checked with
its own node's algorithm, in the CLI's real process pool, through a real
``Drive`` on ``wcpan-drive-client-feed``.
"""

import hashlib
import io
import tempfile
from collections.abc import Iterator
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from typing import Any
from unittest import IsolatedAsyncioTestCase

from aiohttp import web
from aiohttp.test_utils import TestServer
from Crypto.Hash import MD4
from wcpan.drive.client.api import create_drive
from wcpan.drive.client.cli._download import download_list
from wcpan.drive.client.cli._lib import create_executor
from wcpan.drive.client.feed import create_backend
from wcpan.drive.client.types import Node


def _md4(data: bytes) -> str:
    return MD4.new(data).hexdigest()


def _hashlib(name: str, data: bytes) -> str:
    return hashlib.new(name, data).hexdigest()


# name -> (algorithm, digest function)
_FILES = {
    "nas.bin": ("md4", _md4),
    "disk.bin": ("md5", partial(_hashlib, "md5")),
    "sha1.bin": ("sha1", partial(_hashlib, "sha1")),
    "sha256.bin": ("sha256", partial(_hashlib, "sha256")),
}
_CORRUPT = "corrupt.bin"


def _record(
    id: str,
    *,
    parent_id: str | None,
    name: str,
    is_directory: bool = False,
    hash: str = "",
    hash_algorithm: str = "",
    size: int = 0,
) -> dict[str, Any]:
    return {
        "id": id,
        "parent_id": parent_id,
        "name": name,
        "is_directory": is_directory,
        "created_time": 0,
        "modified_time": 0,
        "changed_time": 0,
        "mime_type": "application/x-directory"
        if is_directory
        else "application/octet-stream",
        "hash": hash,
        "hash_algorithm": hash_algorithm,
        "size": size,
        "is_image": False,
        "is_video": False,
        "width": 0,
        "height": 0,
    }


class _FeedServer:
    """Just enough of the feed protocol to sync a tree and download files."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.root = _record("root", parent_id=None, name="")
        self.records = [
            _record("mixed", parent_id="root", name="mixed", is_directory=True)
        ]
        for name, (algorithm, digest) in _FILES.items():
            body = f"content of {name}".encode()
            self._add(name, body, algorithm, digest(body))
        # A digest of other bytes: verification must catch it.
        self._add(_CORRUPT, b"what was sent", "md5", _hashlib("md5", b"other"))

    def _add(self, name: str, body: bytes, algorithm: str, digest: str) -> None:
        self.bodies[name] = body
        self.records.append(
            _record(
                name,
                parent_id="mixed",
                name=name,
                hash=digest,
                hash_algorithm=algorithm,
                size=len(body),
            )
        )

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/v1/root", self._root)
        app.router.add_get("/api/v1/changes", self._changes)
        app.router.add_get("/api/v1/nodes/{id}/download", self._download)
        return app

    async def _root(self, _: web.Request) -> web.Response:
        return web.json_response(self.root)

    async def _changes(self, request: web.Request) -> web.Response:
        changes: list[dict[str, Any]] = []
        if request.query["cursor"] == "0":
            changes = [
                {"removed": False, "node": record}
                for record in [self.root, *self.records]
            ]
        return web.json_response({"cursor": "1", "has_more": False, "changes": changes})

    async def _download(self, request: web.Request) -> web.Response:
        return web.Response(body=self.bodies[request.match_info["id"]])


class TestMixedHashAlgorithms(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.feed = _FeedServer()
        self.server = TestServer(self.feed.app())
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pool = self.enterContext(create_executor())

    async def test_each_file_is_checked_with_its_own_algorithm(self) -> None:
        # given
        failed: list[Node] = []
        backend = partial(create_backend, server_url=str(self.server.make_url("/")))
        async with create_drive(
            backend=backend, cache_path=str(self.tmp / "cache.sqlite")
        ) as drive:
            async for _ in drive.sync():
                pass
            folder = await drive.get_node_by_id("mixed")
            algorithms = {
                child.name: child.hash_algorithm
                for child in await drive.get_children(folder)
            }

            # when: progress lines go to stdout
            with redirect_stdout(io.StringIO()):
                has_error = await download_list(
                    [folder],
                    self.tmp,
                    drive=drive,
                    pool=self.pool,
                    jobs=2,
                    fail_fast=False,
                    include_trash=False,
                    failed=failed,
                )

        # then: the listing really mixes algorithms
        self.assertEqual(set(algorithms.values()), {"md4", "md5", "sha1", "sha256"})
        # then: only the corrupt file fails, though it was written
        self.assertTrue(has_error)
        self.assertEqual([node.name for node in failed], [_CORRUPT])
        for name, body in self._downloaded():
            with self.subTest(name=name):
                self.assertEqual(body, self.feed.bodies[name])

    def _downloaded(self) -> Iterator[tuple[str, bytes]]:
        for name in [*_FILES, _CORRUPT]:
            yield name, (self.tmp / "mixed" / name).read_bytes()
