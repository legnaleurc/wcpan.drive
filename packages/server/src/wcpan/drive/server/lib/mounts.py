"""Virtual-ID grammar and mount-node helpers; private to the core."""

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE, utc_now
from wcpan.drive.protocol.types import NodeId, NodeRecord


VIRTUAL_ID_PREFIX = "_"
SERVER_ROOT_ID = NodeId(VIRTUAL_ID_PREFIX)


def is_virtual(node_id: NodeId) -> bool:
    return str(node_id).startswith(VIRTUAL_ID_PREFIX)


def is_mount_node_id(node_id: NodeId) -> bool:
    """True for mount directory ids; false for the bare root ``_``."""
    return len(str(node_id)) > 1 and str(node_id).startswith(VIRTUAL_ID_PREFIX)


def mount_id(name: str) -> NodeId:
    return NodeId(f"{VIRTUAL_ID_PREFIX}{name}")


def mount_name(node_id: NodeId) -> str | None:
    """Return the mount config key for ``node_id``, or None if not a mount node."""
    if not is_mount_node_id(node_id):
        return None
    return str(node_id)[1:]


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
