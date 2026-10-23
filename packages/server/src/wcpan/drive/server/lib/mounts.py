"""Virtual-ID grammar and mount-node helpers."""

from wcpan.drive.protocol.types import MirrorStableId


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
