"""The server root, and how the core recognizes it and the mount nodes.

The root is the only node without a parent, and mounts are its only children,
so both are recognized by position in the tree, never by their IDs.
"""

from wcpan.drive.protocol.types import NodeId, NodeRecord


SERVER_ROOT_ID = NodeId("_")


def is_structural(record: NodeRecord) -> bool:
    """Whether *record* is the server root or a mount node."""
    return record.parent_id is None or record.parent_id == SERVER_ROOT_ID
