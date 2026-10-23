"""Resolve client-facing virtual paths to node IDs over the mirror."""

from wcpan.drive.protocol.types import NodeId

from .._types import VirtualPath
from .mirror.store import (
    MirrorReader,
)


# ---------------------------------------------------------------------------
# VirtualPathService
# ---------------------------------------------------------------------------


class VirtualPathService:
    """Resolve client-facing virtual paths to directory node IDs."""

    def __init__(self, *, mirror: MirrorReader) -> None:
        self._mirror = mirror

    async def resolve_to_directory_node_id(
        self,
        virtual_path: VirtualPath,
    ) -> NodeId:
        """Resolve a server virtual path (``/`` = the root) to a directory ``node_id``."""
        segments = _virtual_path_segments(virtual_path)

        root = await self._mirror.get_root()
        if root is None:
            raise ValueError("Server root is missing from the database")
        if not root.is_directory:
            raise ValueError("Server root is not a directory in the database")

        if not segments:
            return root.id

        node_id = await self._mirror.resolve_path_to_id(segments)
        if node_id is None:
            raise ValueError(
                f"No directory found at virtual path: {str(virtual_path)!r}"
            )

        return node_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _virtual_path_segments(virtual_path: VirtualPath) -> list[str]:
    """Split a virtual path into directory name segments (no leading/trailing slashes)."""
    raw = str(virtual_path).strip()
    if not raw or raw == "/":
        return []
    parts = [p for p in raw.split("/") if p]
    for p in parts:
        if p in (".", ".."):
            raise ValueError(f"Invalid virtual path segment: {p!r}")
    return parts
