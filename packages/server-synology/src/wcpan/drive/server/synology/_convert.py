"""Convert Synology Drive file info into mirror observations."""

from logging import getLogger

from wcpan.drive.protocol.lib import guess_mime_type
from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.server.types import BackendLocator, NodeObservation
from wcpan.synology import SynologyFileInfo


_L = getLogger(__name__)

SYNOLOGY_HASH_ALGORITHM = "md4"
"""Synology Drive reports a whole-file MD4 hex digest."""

SynologyLocator = BackendLocator
"""Synology ``file_id``; mutable across some operations, unlike the public node ID."""


def convert_file_info(
    info: SynologyFileInfo,
    parent_id: NodeId | None,
) -> NodeObservation | None:
    is_dir = info["type"] == "dir"
    name = info["name"]
    permanent_link = info.get("permanent_link")
    if not permanent_link:
        _L.warning(
            "SynologyFileInfo missing permanent_link for file_id=%s; skipping",
            info["file_id"],
        )
        return None
    hash_ = "" if is_dir else info.get("hash", "")
    record = NodeRecord(
        id=NodeId(permanent_link),
        parent_id=parent_id,
        name=name,
        is_directory=is_dir,
        created_time=info.get("created_time", 0),
        modified_time=info.get("modified_time", 0),
        changed_time=info.get("change_time", 0),
        mime_type=guess_mime_type(name, is_directory=is_dir),
        hash=hash_,
        hash_algorithm=SYNOLOGY_HASH_ALGORITHM if hash_ else "",
        size=info.get("size", 0),
        is_image=info.get("content_type") == "image",
        is_video=info.get("content_type") == "video",
        width=0,
        height=0,
        ms_duration=0,
    )
    return NodeObservation(record=record, locator=SynologyLocator(info["file_id"]))
