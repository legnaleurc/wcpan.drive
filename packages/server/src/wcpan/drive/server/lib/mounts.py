"""Virtual-ID grammar and mount-node helpers."""

from collections.abc import Iterable

from wcpan.drive.protocol.lib import FOLDER_MIME_TYPE, utc_now
from wcpan.drive.protocol.types import MirrorStableId, NodeRecord


VIRTUAL_ID_PREFIX = "_"
SERVER_ROOT_ID = MirrorStableId(VIRTUAL_ID_PREFIX)


def is_virtual(node_id: MirrorStableId) -> bool:
    return str(node_id).startswith(VIRTUAL_ID_PREFIX)


def is_mount_node_id(node_id: MirrorStableId) -> bool:
    """True for mount directory ids; false for the bare root ``_``."""
    return len(str(node_id)) > 1 and str(node_id).startswith(VIRTUAL_ID_PREFIX)


def mount_id(name: str) -> MirrorStableId:
    return MirrorStableId(f"{VIRTUAL_ID_PREFIX}{name}")


def mount_name(node_id: MirrorStableId) -> str | None:
    """Return the mount config key for ``node_id``, or None if not a mount node."""
    if not is_mount_node_id(node_id):
        return None
    return str(node_id)[1:]


def _virtual_directory(
    node_id: MirrorStableId, parent_id: MirrorStableId | None, name: str
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


def structural_records(mount_names: Iterable[str]) -> list[NodeRecord]:
    """The server root and one virtual directory per mount."""
    return [_virtual_directory(SERVER_ROOT_ID, None, "")] + [
        _virtual_directory(mount_id(name), SERVER_ROOT_ID, name) for name in mount_names
    ]
