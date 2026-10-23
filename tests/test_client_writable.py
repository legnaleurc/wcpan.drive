"""Tests for _ResumableWritableFile and create_writable."""

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from wcpan.drive.client.remote.exceptions import (
    SynologyNameTooLongError,
    SynologyUploadError,
)
from wcpan.drive.client.remote.writable import (
    _MAX_SPOOL,
    _ResumableWritableFile,
    create_writable,
)


def _response_cm(status: int, body: dict) -> MagicMock:
    """Build a mock aiohttp context manager that returns a response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    response.raise_for_status = MagicMock()
    response.headers = {}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _head_cm(status: int, *, upload_offset: str = "0") -> MagicMock:
    """Build a mock HEAD response with Upload-Offset header."""
    response = MagicMock()
    response.status = status
    response.raise_for_status = MagicMock()
    response.headers = {"Upload-Offset": upload_offset}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _patch_cm(
    status: int,
    *,
    upload_offset: str = "0",
    retry_after: str | None = None,
    body: dict | None = None,
) -> MagicMock:
    """Build a mock PATCH response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=body or {})
    response.raise_for_status = MagicMock()
    response.headers = {"Upload-Offset": upload_offset}
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _node_dict() -> dict:
    return {
        "id": "node-1",
        "mutable_id": "node-1",
        "parent_id": "parent-1",
        "name": "file.bin",
        "is_directory": False,
        "created_time": 1_704_067_200,
        "modified_time": 1_704_067_200,
        "changed_time": 1_704_067_200,
        "mime_type": "application/octet-stream",
        "hash": "abc123",
        "size": 100,
        "is_image": False,
        "is_video": False,
        "width": 0,
        "height": 0,
        "ms_duration": 0,
    }


def _make_session(
    session_id: str = "test-session",
    total_size: int = 100,
) -> MagicMock:
    """Build a mock aiohttp ClientSession pre-wired for resumable upload."""
    session = MagicMock(spec=aiohttp.ClientSession)
    # POST /nodes/{parent_id}/uploads → 201 + Location header
    post_resp = MagicMock()
    post_resp.status = 201
    post_resp.raise_for_status = MagicMock()
    post_resp.headers = {"Location": f"/api/v1/uploads/{session_id}"}
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=post_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=post_cm)
    # HEAD /uploads/{id} → 200 with Upload-Offset: 0 (nothing received yet)
    session.head = MagicMock(return_value=_head_cm(200, upload_offset="0"))
    # PATCH /uploads/{id} → 201 with node (successful upload)
    session.patch = MagicMock(return_value=_patch_cm(201, body=_node_dict()))
    # DELETE /uploads/{id} → 204
    session.delete = MagicMock(return_value=_response_cm(204, {}))
    return session


# ---------- _ResumableWritableFile ----------


def _make_writable(
    client,
    session_id: str = "sid",
    total_size: int = 100,
    buf: tempfile.SpooledTemporaryFile | None = None,
) -> _ResumableWritableFile:
    if buf is None:
        buf = tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b")
    session_url = f"http://srv/api/v1/uploads/{session_id}"
    return _ResumableWritableFile(client, session_url, total_size, "f.bin", buf)


class TestResumableWritableFileFlush(IsolatedAsyncioTestCase):
    async def test_flush_sends_data_and_returns_node(self):
        # given
        data = b"x" * 100
        client = _make_session("sid", total_size=100)
        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid", 100, buf)

            # when
            await writable.write(data)
            await writable.flush()

            # then — PATCH was called with Upload-Offset: 0
            node = await writable.node()
        self.assertEqual(node.id, "node-1")
        client.patch.assert_called_once()
        _, patch_kwargs = client.patch.call_args
        self.assertIn("Upload-Offset", patch_kwargs.get("headers", {}))
        self.assertEqual(patch_kwargs["headers"]["Upload-Offset"], "0")

    async def test_flush_is_idempotent(self):
        data = b"y" * 50
        client = _make_session("sid2", total_size=50)
        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid2", 50, buf)
            await writable.write(data)
            await writable.flush()
            # second flush should not re-upload
            await writable.flush()
        self.assertEqual(client.patch.call_count, 1)

    async def test_flush_sends_single_patch(self):
        total = 1024
        data = b"z" * total

        client = _make_session("sid3", total_size=total)
        client.patch = MagicMock(return_value=_patch_cm(201, body=_node_dict()))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid3", total, buf)
            await writable.write(data)
            await writable.flush()

        self.assertEqual(client.patch.call_count, 1)
        _, kw = client.patch.call_args
        self.assertEqual(kw["headers"]["Upload-Offset"], "0")


class TestResumableWritableFileRetry(IsolatedAsyncioTestCase):
    async def test_422_name_too_long_is_typed_and_not_retried(self):
        client = _make_session("sid-long", total_size=50)
        client.patch = MagicMock(
            return_value=_response_cm(
                422,
                {
                    "error": "name_too_long",
                    "message": "File name is too long",
                    "name": "long.bin",
                },
            )
        )

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-long", 50, buf)
            await writable.write(b"x" * 50)
            with self.assertRaises(SynologyNameTooLongError) as ctx:
                await writable.flush()

        self.assertEqual(ctx.exception.file_name, "long.bin")
        client.patch.assert_called_once()

    async def test_503_honors_retry_after_and_resumes(self):
        total = 100
        client = _make_session("sid-503-resume", total_size=total)
        client.patch = MagicMock(
            side_effect=[
                _patch_cm(503, retry_after="7"),
                _patch_cm(201, body=_node_dict()),
            ]
        )
        client.head = MagicMock(return_value=_head_cm(200, upload_offset="40"))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-503-resume", total, buf)
            await writable.write(b"a" * total)
            with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("wcpan.drive.client.remote.writable", "WARNING"):
                    await writable.flush()

        sleep.assert_awaited_once_with(7.0)
        _, retry_kwargs = client.patch.call_args_list[1]
        self.assertEqual(retry_kwargs["headers"]["Upload-Offset"], "40")
        self.assertEqual(retry_kwargs["headers"]["Content-Length"], "60")
        self.assertIsNot(retry_kwargs["data"], buf)

    async def test_offset_mismatch_seeks_and_retries(self):
        # Server says it has 50 bytes when client thought it had 0.
        total = 100
        client = _make_session("sid-mismatch", total_size=total)
        # First PATCH → 409 with Upload-Offset showing server has 50 bytes
        # Second PATCH → 201 (success from offset 50)
        client.patch = MagicMock(
            side_effect=[
                _patch_cm(409, upload_offset="50"),
                _patch_cm(201, body=_node_dict()),
            ]
        )
        client.head = MagicMock(return_value=_head_cm(200, upload_offset="0"))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-mismatch", total, buf)
            await writable.write(b"a" * 100)
            await writable.flush()

        self.assertEqual(client.patch.call_count, 2)
        # Second PATCH must start at offset 50
        _, kw2 = client.patch.call_args_list[1]
        self.assertEqual(kw2["headers"]["Upload-Offset"], "50")

    async def test_connection_error_retries_from_server_received(self):
        total = 100
        client = _make_session("sid-retry", total_size=total)
        # First PATCH raises ClientError; second PATCH succeeds.
        client.patch = MagicMock(
            side_effect=[
                _cm_raises(aiohttp.ClientError("connection reset")),
                _patch_cm(201, body=_node_dict()),
            ]
        )
        # HEAD → server received 40 bytes before the drop
        client.head = MagicMock(return_value=_head_cm(200, upload_offset="40"))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-retry", total, buf)
            await writable.write(b"b" * 100)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertLogs("wcpan.drive.client.remote.writable", "WARNING"):
                    await writable.flush()

        self.assertEqual(client.patch.call_count, 2)
        _, kw2 = client.patch.call_args_list[1]
        self.assertEqual(kw2["headers"]["Upload-Offset"], "40")

    async def test_503_retries_with_backoff(self):
        client = _make_session("sid-503", total_size=50)
        client.patch = MagicMock(return_value=_response_cm(503, {}))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-503", 50, buf)
            await writable.write(b"c" * 50)
            with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertLogs("wcpan.drive.client.remote.writable", "WARNING"):
                    with self.assertRaises(SynologyUploadError):
                        await writable.flush()

        self.assertEqual(client.patch.call_count, 6)
        self.assertEqual(sleep.await_count, 5)

    async def test_404_raises_upload_error(self):
        client = _make_session("sid-404", total_size=50)
        client.patch = MagicMock(return_value=_response_cm(404, {}))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-404", 50, buf)
            await writable.write(b"d" * 50)
            with self.assertRaises(SynologyUploadError):
                await writable.flush()

    async def test_exceeds_max_retries_raises_upload_error(self):
        from wcpan.drive.client.remote.writable import _MAX_RETRIES

        total = 50
        client = _make_session("sid-maxretry", total_size=total)
        client.patch = MagicMock(return_value=_cm_raises(aiohttp.ClientError("drop")))
        client.head = MagicMock(return_value=_head_cm(200, upload_offset="0"))

        with tempfile.SpooledTemporaryFile(max_size=_MAX_SPOOL, mode="w+b") as buf:
            writable = _make_writable(client, "sid-maxretry", total, buf)
            await writable.write(b"e" * total)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with self.assertLogs("wcpan.drive.client.remote.writable", "WARNING"):
                    with self.assertRaises(SynologyUploadError):
                        await writable.flush()

        self.assertEqual(client.patch.call_count, _MAX_RETRIES)


# ---------- create_writable ----------


class TestCreateWritable(IsolatedAsyncioTestCase):
    async def test_size_positive_uses_resumable_path(self):
        client = _make_session("sess-1", total_size=50)
        async with create_writable(
            session=client,
            server_url="http://srv",
            parent_id="par",
            name="f.bin",
            size=50,
            mime_type=None,
        ) as w:
            await w.write(b"a" * 50)
        # session initiation via POST with JSON body
        client.post.assert_called_once()
        args, kwargs = client.post.call_args
        self.assertIn("/uploads", args[0])
        self.assertIn("json", kwargs)

    async def test_size_zero_uses_empty_path(self):
        client = MagicMock(spec=aiohttp.ClientSession)
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value=_node_dict())
        response.raise_for_status = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        client.post = MagicMock(return_value=cm)

        async with create_writable(
            session=client,
            server_url="http://srv",
            parent_id="par",
            name="empty.bin",
            size=0,
            mime_type=None,
        ) as w:
            pass  # no writes for empty file

        # Must POST to /api/v1/nodes/{parent_id} (no /uploads suffix)
        args, _ = client.post.call_args
        self.assertIn("/api/v1/nodes/par", args[0])
        self.assertNotIn("/uploads", args[0])

    async def test_exception_cancels_session(self):
        client = _make_session("sess-cancel", total_size=100)

        with self.assertRaises(ValueError):
            async with create_writable(
                session=client,
                server_url="http://srv",
                parent_id="par",
                name="f.bin",
                size=100,
                mime_type=None,
            ) as w:
                raise ValueError("caller error")

        # DELETE must have been called to clean up session
        client.delete.assert_called_once()


# ---------- helpers ----------


def _cm_raises(exc: Exception) -> MagicMock:
    """Build a mock context manager whose __aenter__ raises exc."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm
