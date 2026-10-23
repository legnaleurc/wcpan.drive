import mimetypes
import re
from datetime import UTC, datetime
from typing import Literal, TypedDict

from .types import NodeId, NodeRecord


__all__ = (
    "FOLDER_MIME_TYPE",
    "NodeRecordDict",
    "RemovedChangeDict",
    "UpsertChangeDict",
    "ChangeDict",
    "node_record_to_dict",
    "node_record_from_dict",
    "parse_content_range",
    "guess_mime_type",
    "utc_now",
    "utc_from_timestamp",
)


FOLDER_MIME_TYPE = "application/x-directory"


class NodeRecordDict(TypedDict):
    id: str
    parent_id: str | None
    name: str
    is_directory: bool
    created_time: int
    modified_time: int
    changed_time: int
    mime_type: str
    hash: str
    hash_algorithm: str
    size: int
    is_image: bool
    is_video: bool
    width: int
    height: int


class RemovedChangeDict(TypedDict):
    removed: Literal[True]
    node_id: str


class UpsertChangeDict(TypedDict):
    removed: Literal[False]
    node: NodeRecordDict


type ChangeDict = RemovedChangeDict | UpsertChangeDict


def node_record_to_dict(record: NodeRecord) -> NodeRecordDict:
    return {
        "id": str(record.id),
        "parent_id": str(record.parent_id) if record.parent_id is not None else None,
        "name": record.name,
        "is_directory": record.is_directory,
        "created_time": record.created_time,
        "modified_time": record.modified_time,
        "changed_time": record.changed_time,
        "mime_type": record.mime_type,
        "hash": record.hash,
        "hash_algorithm": record.hash_algorithm,
        "size": record.size,
        "is_image": record.is_image,
        "is_video": record.is_video,
        "width": record.width,
        "height": record.height,
    }


def node_record_from_dict(data: NodeRecordDict) -> NodeRecord:
    parent_id = data["parent_id"]
    return NodeRecord(
        id=NodeId(data["id"]),
        parent_id=NodeId(parent_id) if parent_id is not None else None,
        name=data["name"],
        is_directory=data["is_directory"],
        created_time=data["created_time"],
        modified_time=data["modified_time"],
        changed_time=data["changed_time"],
        mime_type=data["mime_type"],
        hash=data["hash"],
        hash_algorithm=data["hash_algorithm"],
        size=data["size"],
        is_image=data["is_image"],
        is_video=data["is_video"],
        width=data["width"],
        height=data["height"],
    )


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def parse_content_range(value: str) -> tuple[int, int, int | None] | None:
    """The first byte, last byte, and total size in a 206 ``Content-Range``.

    Accepts only a valid satisfied byte range (RFC 9110 §14.4), such as
    ``bytes 0-99/1000``; the total is ``None`` for ``*``. Anything else gives
    ``None``.
    """
    match = _CONTENT_RANGE.fullmatch(value)
    if match is None:
        return None
    first, last = int(match[1]), int(match[2])
    total = None if match[3] == "*" else int(match[3])
    if last < first or (total is not None and last >= total):
        return None
    return first, last, total


def guess_mime_type(name: str, *, is_directory: bool) -> str:
    if is_directory:
        return FOLDER_MIME_TYPE
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def utc_now() -> int:
    return int(datetime.now(UTC).timestamp())


def utc_from_timestamp(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)
