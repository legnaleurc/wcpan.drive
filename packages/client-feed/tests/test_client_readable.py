"""Tests for ClientReadableFile."""

import re
from datetime import UTC, datetime

from aiohttp.test_utils import AioHTTPTestCase
from aiohttp.web import Application, Request, Response
from wcpan.drive.client.feed._readable import ClientReadableFile
from wcpan.drive.client.feed.exceptions import SynologyServerError
from wcpan.drive.client.types import Node


_BODY = bytes(range(256)) * 4 + b"end"
_RANGE = re.compile(r"bytes=(\d+)-(\d+)")


def _file_node(size: int) -> Node:
    t = datetime.now(UTC)
    return Node(
        id="f",
        parent_id="p",
        name="file.bin",
        is_directory=False,
        is_trashed=False,
        created_time=t,
        modified_time=t,
        changed_time=t,
        mime_type="application/octet-stream",
        hash="",
        hash_algorithm="",
        size=size,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        private=None,
    )


class TestClientReadableFile(AioHTTPTestCase):
    async def get_application(self) -> Application:
        self.ranges: list[str | None] = []
        # Replaces the answer to a ranged request, to play a faulty server.
        self.ranged_answer: Response | None = None
        app = Application()
        app.router.add_get("/api/v1/nodes/f/download", self._download)
        return app

    async def _download(self, request: Request) -> Response:
        # Like the server: an unsatisfiable or reversed range is ignored.
        header = request.headers.get("Range")
        self.ranges.append(header)
        match = _RANGE.fullmatch(header or "")
        if match and self.ranged_answer is not None:
            return self.ranged_answer
        if match:
            start, end = int(match[1]), int(match[2])
            if start <= end < len(_BODY):
                return Response(
                    status=206,
                    body=_BODY[start : end + 1],
                    headers={"Content-Range": f"bytes {start}-{end}/{len(_BODY)}"},
                )
        return Response(body=_BODY)

    def _open(self) -> ClientReadableFile:
        return ClientReadableFile(
            _file_node(len(_BODY)),
            session=self.client.session,
            server_url=str(self.client.make_url("/")),
        )

    async def test_seek_to_end_reads_nothing_without_a_request(self) -> None:
        async with self._open() as f:
            self.assertEqual(await f.seek(len(_BODY)), len(_BODY))
            self.assertEqual(await f.read(1), b"")
            self.assertEqual([_ async for _ in f], [])
        self.assertEqual(self.ranges, [])

    async def test_seek_past_end_reads_nothing(self) -> None:
        async with self._open() as f:
            await f.seek(len(_BODY) + 10)
            self.assertEqual(await f.read(1), b"")
        self.assertEqual(self.ranges, [])

    async def test_read_to_end_then_returns_nothing(self) -> None:
        async with self._open() as f:
            await f.seek(len(_BODY) - 3)
            self.assertEqual(await f.read(10), b"end")
            self.assertEqual(await f.read(1), b"")
        self.assertEqual(self.ranges, [f"bytes={len(_BODY) - 3}-{len(_BODY) - 1}"])

    async def test_seek_back_from_end_reads_again(self) -> None:
        async with self._open() as f:
            await f.seek(len(_BODY))
            await f.seek(0)
            self.assertEqual(await f.read(3), _BODY[:3])
        self.assertEqual(self.ranges, [None])

    async def test_seek_reads_from_the_offset(self) -> None:
        async with self._open() as f:
            await f.seek(10)
            self.assertEqual(await f.read(3), _BODY[10:13])
        self.assertEqual(self.ranges, [f"bytes=10-{len(_BODY) - 1}"])

    async def test_range_of_a_changed_file_is_accepted(self) -> None:
        # given: the server's copy grew; the positions still match
        last = len(_BODY) - 1
        self.ranged_answer = Response(
            status=206,
            body=_BODY[10:],
            headers={"Content-Range": f"bytes 10-{last}/{len(_BODY) + 5}"},
        )

        # when
        async with self._open() as f:
            await f.seek(10)
            chunk = await f.read(3)

        # then
        self.assertEqual(chunk, _BODY[10:13])

    async def test_whole_file_for_a_range_fails(self) -> None:
        # given: a server that ignores Range
        self.ranged_answer = Response(body=_BODY)

        # when / then
        async with self._open() as f:
            with self.assertRaises(SynologyServerError) as cm:
                await f.seek(10)
        self.assertEqual(cm.exception.status, 200)

    async def test_other_range_fails(self) -> None:
        # given
        last = len(_BODY) - 1
        self.ranged_answer = Response(
            status=206,
            body=_BODY[11:],
            headers={"Content-Range": f"bytes 11-{last}/{len(_BODY)}"},
        )

        # when / then
        async with self._open() as f:
            with self.assertRaises(SynologyServerError):
                await f.seek(10)

    async def test_range_without_content_range_fails(self) -> None:
        # given
        self.ranged_answer = Response(status=206, body=_BODY[10:])

        # when / then
        async with self._open() as f:
            with self.assertRaises(SynologyServerError):
                await f.seek(10)
