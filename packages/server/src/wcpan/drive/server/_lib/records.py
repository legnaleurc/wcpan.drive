"""Comparisons of node records."""

from dataclasses import replace

from wcpan.drive.protocol.types import NodeRecord


def same_public_metadata(a: NodeRecord, b: NodeRecord) -> bool:
    """Equal apart from the ID and probed media dimensions.

    Listings do not report dimensions, which come from probing.
    """
    return replace(a, id=b.id, width=b.width, height=b.height) == b
