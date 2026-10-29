"""Tests for _ResumableWritableFile and create_writable."""

import tempfile
from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from decoy import matchers
from wcpan.drive.client.feed.exceptions import (
    SynologyNameTooLongError,
    SynologyUploadError,
)
from wcpan.drive.client.feed.writable import (
    _MAX_RETRIES,
    _MAX_SPOOL,
    _ResumableWritableFile,
    create_writable,
)

from ._lib import DecoyTestCase


_SESSION_PATH = "/api/v1/uploads/sid"
_SESSION_URL = f"http://srv{_SESSION_PATH}"


def _node_dict() -> dict[str, Any]:
    return {
        "id": "node-1",
        "parent_id": "parent-1",
        "name": "file.bin",
        "is_directory": False,
        "created_time": 1_704_067_200,
        "modified_time": 1_704_067_200,
        "changed_time": 1_704_067_200,
        "mime_type": "application/octet-stream",
        "hash": "abc123",
        "hash_algorithm": "md4",
        "size": 100,
        "is_image": False,
        "is_video": False,
        "width": 0,
        "height": 0,
        "ms_duration": 0,
    }


def _head(
    session: aiohttp.ClientSession,
) -> AbstractAsyncContextManager[aiohttp.ClientResponse]:
    return session.head(_SESSION_URL, timeout=matchers.Anything())


def _patch(
    session: aiohttp.ClientSession,
    offset: int | None = None,
    *,
    length: int | None = None,
    data: Any = matchers.Anything(),
) -> AbstractAsyncContextManager[aiohttp.ClientResponse]:
    """Rehearse a PATCH; ``None`` offset matches any offset."""
    headers: dict[str, str] = {}
    if offset is not None:
        headers["Upload-Offset"] = str(offset)
    if length is not None:
        headers["Content-Length"] = str(length)
    return session.patch(
        _SESSION_URL,
        data=data,
        headers=matchers.DictMatching(headers),
        timeout=matchers.Anything(),
    )


def _spool() -> tempfile.SpooledTemporaryFile[bytes]:
    return tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b")


def _make_writable(
    session: aiohttp.ClientSession,
    total_size: int,
    buf: tempfile.SpooledTemporaryFile[bytes],
) -> _ResumableWritableFile:
    return _ResumableWritableFile(session, _SESSION_URL, total_size, "f.bin", buf)


class _WritableTestCase(DecoyTestCase):
    async def asyncSetUp(self) -> None:
        self.session = self.decoy.mock(cls=aiohttp.ClientSession)
        self.created = await self.create_response(201, body=_node_dict())
        # HEAD → nothing received yet
        received = await self._received(0)
        self.when_enter(_head(self.session), received)
        # PATCH from 0 → 201 with node (successful upload)
        self.when_enter(_patch(self.session, 0), self.created)

    async def _received(self, offset: int) -> aiohttp.ClientResponse:
        return await self.create_response(200, headers={"Upload-Offset": str(offset)})


# ---------- _ResumableWritableFile ----------


class TestResumableWritableFileFlush(_WritableTestCase):
    async def test_flush_sends_data_and_returns_node(self):
        # given
        with _spool() as buf:
            writable = _make_writable(self.session, 100, buf)

            # when
            await writable.write(b"x" * 100)
            await writable.flush()

            # then
            node = await writable.node()
        self.assertEqual(node.id, "node-1")
        self.decoy.verify(_patch(self.session, 0), times=1)

    async def test_flush_is_idempotent(self):
        # given
        with _spool() as buf:
            writable = _make_writable(self.session, 50, buf)
            # when
            await writable.write(b"y" * 50)
            await writable.flush()
            # second flush should not re-upload
            await writable.flush()
        # then
        self.decoy.verify(_patch(self.session), times=1)

    async def test_flush_sends_single_patch(self):
        # given
        total = 1024

        with _spool() as buf:
            writable = _make_writable(self.session, total, buf)
            # when
            await writable.write(b"z" * total)
            await writable.flush()

        # then
        self.decoy.verify(_patch(self.session), times=1)
        self.decoy.verify(_patch(self.session, 0, length=total), times=1)


class TestResumableWritableFileRetry(_WritableTestCase):
    async def test_422_name_too_long_is_typed_and_not_retried(self):
        # given
        too_long = await self.create_response(
            422,
            body={
                "error": "name_too_long",
                "message": "File name is too long",
                "name": "long.bin",
            },
        )
        self.when_enter(_patch(self.session, 0), too_long)

        with _spool() as buf:
            writable = _make_writable(self.session, 50, buf)
            # when
            await writable.write(b"x" * 50)
            with self.assertRaises(SynologyNameTooLongError) as ctx:
                await writable.flush()

        # then
        self.assertEqual(ctx.exception.file_name, "long.bin")
        self.decoy.verify(_patch(self.session), times=1)

    async def test_503_honors_retry_after_and_resumes(self):
        # given
        total = 100
        # HEAD → 0 before the first PATCH, 40 after the 503
        received = [await self._received(0), await self._received(40)]
        unavailable = await self.create_response(503, headers={"Retry-After": "7"})
        self.when_enter(_head(self.session), *received)
        self.when_enter(_patch(self.session, 0), unavailable)
        self.when_enter(_patch(self.session, 40), self.created)

        with _spool() as buf:
            writable = _make_writable(self.session, total, buf)
            # when
            await writable.write(b"a" * total)
            with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("wcpan.drive.client.feed.writable", "WARNING"):
                    await writable.flush()

        # then
        sleep.assert_awaited_once_with(7.0)
        self.decoy.verify(
            _patch(self.session, 40, length=60, data=matchers.IsNot(buf)),
            times=1,
        )

    async def test_offset_mismatch_seeks_and_retries(self):
        # given
        # Server says it has 50 bytes when client thought it had 0.
        conflict = await self.create_response(409, headers={"Upload-Offset": "50"})
        self.when_enter(_patch(self.session, 0), conflict)
        self.when_enter(_patch(self.session, 50), self.created)

        with _spool() as buf:
            writable = _make_writable(self.session, 100, buf)
            # when
            await writable.write(b"a" * 100)
            await writable.flush()

        # then
        self.decoy.verify(_patch(self.session), times=2)
        self.decoy.verify(_patch(self.session, 50), times=1)

    async def test_connection_error_retries_from_server_received(self):
        # given
        # HEAD → server received 40 bytes before the drop
        received = [await self._received(0), await self._received(40)]
        self.when_enter(_head(self.session), *received)
        self.when_enter(_patch(self.session, 40), self.created)
        self.decoy.when(_patch(self.session, 0)).then_raise(
            aiohttp.ClientError("connection reset")
        )

        with _spool() as buf:
            writable = _make_writable(self.session, 100, buf)
            # when
            await writable.write(b"b" * 100)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertLogs("wcpan.drive.client.feed.writable", "WARNING"):
                    await writable.flush()

        # then
        self.decoy.verify(_patch(self.session, 40), times=1)

    async def test_503_retries_with_backoff(self):
        # given
        unavailable = await self.create_response(503)
        self.when_enter(_patch(self.session, 0), unavailable)

        with _spool() as buf:
            writable = _make_writable(self.session, 50, buf)
            # when
            await writable.write(b"c" * 50)
            with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("wcpan.drive.client.feed.writable", "WARNING"):
                    with self.assertRaises(SynologyUploadError):
                        await writable.flush()

        # then
        self.decoy.verify(_patch(self.session), times=6)
        self.assertEqual(sleep.await_count, 5)

    async def test_404_raises_upload_error(self):
        # given
        not_found = await self.create_response(404)
        self.when_enter(_patch(self.session, 0), not_found)

        with _spool() as buf:
            writable = _make_writable(self.session, 50, buf)
            # when
            await writable.write(b"d" * 50)
            # then
            with self.assertRaises(SynologyUploadError):
                await writable.flush()

    async def test_exceeds_max_retries_raises_upload_error(self):
        # given
        self.decoy.when(_patch(self.session, 0)).then_raise(aiohttp.ClientError("drop"))

        with _spool() as buf:
            writable = _make_writable(self.session, 50, buf)
            # when
            await writable.write(b"e" * 50)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertLogs("wcpan.drive.client.feed.writable", "WARNING"):
                    with self.assertRaises(SynologyUploadError):
                        await writable.flush()

        # then
        self.decoy.verify(_patch(self.session, 0), times=_MAX_RETRIES)


# ---------- create_writable ----------


class TestCreateWritable(_WritableTestCase):
    async def _stub_initiate(self, size: int) -> None:
        initiated = await self.create_response(201, headers={"Location": _SESSION_PATH})
        self.when_enter(
            self.session.post(
                "http://srv/api/v1/nodes/par/uploads",
                json={"name": "f.bin", "size": size},
            ),
            initiated,
        )

    async def test_size_positive_uses_resumable_path(self):
        # given
        await self._stub_initiate(50)

        # when
        async with create_writable(
            session=self.session,
            server_url="http://srv",
            parent_id="par",
            name="f.bin",
            size=50,
            mime_type=None,
        ) as w:
            await w.write(b"a" * 50)

        # then
        self.assertEqual((await w.node()).id, "node-1")
        self.decoy.verify(_patch(self.session, 0, length=50), times=1)

    async def test_size_zero_uses_empty_path(self):
        # given
        # Must POST to /api/v1/nodes/{parent_id} (no /uploads suffix)
        uploaded = await self.create_response(200, body=_node_dict())
        self.when_enter(
            self.session.post(
                "http://srv/api/v1/nodes/par",
                data=b"",
                params={"name": "empty.bin"},
            ),
            uploaded,
        )

        # when
        async with create_writable(
            session=self.session,
            server_url="http://srv",
            parent_id="par",
            name="empty.bin",
            size=0,
            mime_type=None,
        ) as w:
            pass  # no writes for empty file

        # then
        self.assertEqual((await w.node()).id, "node-1")

    async def test_exception_cancels_session(self):
        # given
        await self._stub_initiate(100)

        # when / then
        with self.assertRaises(ValueError):
            async with create_writable(
                session=self.session,
                server_url="http://srv",
                parent_id="par",
                name="f.bin",
                size=100,
                mime_type=None,
            ):
                raise ValueError("caller error")

        # then
        # DELETE must have been called to clean up session
        self.decoy.verify(self.session.delete(_SESSION_URL), times=1)
