"""The server root and mount nodes; private to the core.

The root is the only node without a parent, and mounts are its only children,
so both are recognized by position in the tree, never by their IDs.
"""

import uuid

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE, utc_now
from wcpan.drive.protocol.types import NodeId, NodeRecord


SERVER_ROOT_ID = NodeId("_")


def is_structural(record: NodeRecord) -> bool:
    """Whether *record* is the server root or a mount node."""
    return record.parent_id is None or record.parent_id == SERVER_ROOT_ID


def new_mount_id() -> NodeId:
    """An ID for a mount seen for the first time."""
    return NodeId(uuid.uuid4().hex)


def mount_id(name: str) -> NodeId:
    """A fixed ID derived from *name*, for mounts built without the mirror."""
    return NodeId(f"_{name}")


def virtual_directory(
    node_id: NodeId, parent_id: NodeId | None, name: str
) -> NodeRecord:
    now = utc_now()
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=name,
        is_directory=True,
        created_time=now,
        modified_time=now,
        changed_time=now,
        mime_type=FOLDER_MIME_TYPE,
        hash="",
        hash_algorithm="",
        size=0,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
    )
