"""Serve one file of a known size over HTTP, with single byte ranges.

Shared by node downloads and the media probe's source service. The caller gives
the size and opens the bytes; this module resolves ``Range`` against that size,
asks for exactly the selected bytes, and keeps a failed source from looking like
a complete short body.
"""

import re
from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from aiohttp import hdrs, web
from aiohttp.client_exceptions import ClientConnectionResetError


# Open exactly *length* bytes at *offset*, which lie within the size given to
# ``serve_range``. Errors raised while opening reach the caller of
# ``serve_range`` before any response is sent.
type OpenRange = Callable[[int, int], AbstractAsyncContextManager[AsyncIterable[bytes]]]


class RangeSourceError(Exception):
    """The source failed, or ended short or long, after the status was sent.

    Escaping the handler makes aiohttp abort the connection, so the client sees
    a failed transfer instead of a complete-looking short one. aiohttp logs it
    with its traceback, so it names only the source exception's type and does
    not chain it: upstream errors can carry URLs with credentials.
    """


# Called with a description of a source failure, and the source's exception if
# it raised one.
type OnSourceFailure = Callable[[str, Exception | None], None]

_ZERO_SUFFIX = re.compile(r"^\s*bytes\s*=\s*-0+\s*$")


async def serve_range(
    request: web.Request,
    *,
    size: int,
    content_type: str,
    open_range: OpenRange,
    on_source_failure: OnSourceFailure | None = None,
) -> web.StreamResponse:
    """Answer GET or HEAD for a file of *size* bytes.

    HEAD is answered from *size* alone, without opening the source; so is an
    empty selection. A ``Range`` that is not one valid byte range (malformed,
    reversed, or several ranges) is ignored, as HTTP allows, and the whole file
    is sent. ``Range`` is also ignored on HEAD and alongside ``If-Range``, as
    HTTP requires. A source failure after the headers calls *on_source_failure* and
    raises ``RangeSourceError``. A client disconnect is normal: the response is
    returned as it is.
    """
    range_ = _requested_range(request)
    selected = range(size)[range_ or slice(None)]
    if range_ is not None and (not selected or range_ == _UNSATISFIABLE):
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"}
        )

    response = web.StreamResponse(
        status=206 if range_ is not None else 200,
        headers={"Content-Type": content_type, "Accept-Ranges": "bytes"},
    )
    # A known length lets the client tell a short body from a complete one,
    # whatever made the stream end early.
    response.content_length = len(selected)
    if range_ is not None:
        response.headers["Content-Range"] = (
            f"bytes {selected.start}-{selected.stop - 1}/{size}"
        )

    if request.method == "HEAD" or not selected:
        try:
            await response.prepare(request)
            await response.write_eof()
        except (ConnectionError, ClientConnectionResetError):
            pass
        return response

    failure: str | None = None
    cause: Exception | None = None
    async with open_range(selected.start, len(selected)) as chunks:
        if not await _send(response.prepare(request)):
            return response
        expected = len(selected)
        sent = 0
        iterator = aiter(chunks)
        while True:
            # Only an error from the source is a source failure; the same
            # exception types from writing the response mean the client left.
            try:
                chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            except Exception as e:
                failure = (
                    f"source failed after {sent} of {expected} bytes"
                    f" ({type(e).__name__})"
                )
                cause = e
                break
            if sent + len(chunk) > expected:
                failure = f"source sent more than {expected} bytes"
                break
            if not await _send(response.write(chunk)):
                return response
            sent += len(chunk)
        if failure is None and sent < expected:
            failure = f"source ended after {sent} of {expected} bytes"

    if failure is not None:
        if on_source_failure is not None:
            on_source_failure(failure, cause)
        raise RangeSourceError(failure) from None

    await _send(response.write_eof())
    return response


# A zero-length suffix (``bytes=-0``), which no file satisfies.
_UNSATISFIABLE = slice(0, 0)


def _requested_range(request: web.Request) -> slice | None:
    header = request.headers.get(hdrs.RANGE)
    if header is None:
        return None
    if request.method != hdrs.METH_GET:
        # GET is the only method with range semantics (RFC 9110 §14.2).
        return None
    if hdrs.IF_RANGE in request.headers:
        # No validator is sent, so no If-Range condition holds, and a false one
        # means the whole file (RFC 9110 §13.1.5).
        return None
    if _ZERO_SUFFIX.match(header):
        # aiohttp reads it as the whole file.
        return _UNSATISFIABLE
    try:
        return request.http_range
    except ValueError:
        return None


async def _send(write: Awaitable[object]) -> bool:
    """Await a response write; ``False`` if the client has disconnected."""
    try:
        await write
    except (ConnectionError, ClientConnectionResetError):
        return False
    return True
