"""Path services over the mirror.

- ``LocalPathService``    — resolve node records to host-filesystem paths
                            (holds *mirror* and *MountSource* by mount node ID)
- ``VirtualPathService``  — resolve client-facing virtual paths to node IDs
                            (holds *mirror*)
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..lib.mounts import SERVER_ROOT_ID, is_virtual
from ..types import VirtualPath
from .mirror import MirrorStore


@dataclass(frozen=True, kw_only=True)
class MountSource:
    """A mount's source, and how its backend maps source paths to host paths."""

    source: str
    local_path: Callable[[str], Path | None]


def map_by_prefix(local_paths: Mapping[str, str]) -> Callable[[str], Path | None]:
    """Map source paths to host paths by the longest matching prefix."""

    def local_path(source_path: str) -> Path | None:
        best = max(
            (p for p in local_paths if source_path.startswith(p)),
            key=len,
            default=None,
        )
        if best is None:
            return None
        return Path(local_paths[best] + source_path[len(best) :])

    return local_path


# ---------------------------------------------------------------------------
# LocalPathService
# ---------------------------------------------------------------------------


class LocalPathService:
    """Resolve node records to host-filesystem paths for media probing.

    Absorbs the ancestor-fetching logic (formerly in the media enricher)
    so callers just pass a ``NodeRecord``.
    """

    def __init__(
        self,
        *,
        mirror: MirrorStore,
        mounts: Mapping[NodeId, MountSource],
    ) -> None:
        self._mirror = mirror
        self._mounts = mounts

    async def resolve_local_path(self, record: NodeRecord) -> Path | None:
        """Reconstruct the local filesystem path for *record*, or ``None``."""
        if not self._mounts:
            return None
        node_cache: dict[str, NodeRecord | None]
        if not record.parent_id or is_virtual(record.parent_id):
            node_cache = {}
        else:
            ancestors = await self._mirror.get_ancestors(record.parent_id)
            node_cache = {a.id: a for a in ancestors}

        return _resolve_local_path(self._mounts, record, node_cache)


# ---------------------------------------------------------------------------
# VirtualPathService
# ---------------------------------------------------------------------------


class VirtualPathService:
    """Resolve client-facing virtual paths to directory node IDs."""

    def __init__(self, *, mirror: MirrorStore) -> None:
        self._mirror = mirror

    async def resolve_to_directory_node_id(
        self,
        virtual_path: VirtualPath,
    ) -> NodeId:
        """Resolve a server virtual path (``/`` = root ``_``) to a directory ``node_id``."""
        segments = _virtual_path_segments(virtual_path)

        root = await self._mirror.get_node_by_id(SERVER_ROOT_ID)
        if root is None:
            raise ValueError("Server root is missing from the database")
        if not root.is_directory:
            raise ValueError("Server root is not a directory in the database")

        if not segments:
            return SERVER_ROOT_ID

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


def _resolve_local_path(
    mounts: Mapping[NodeId, MountSource],
    record: NodeRecord,
    node_cache: dict[str, NodeRecord | None],
) -> Path | None:
    """Walk the pre-fetched ancestor cache to reconstruct a local filesystem path."""
    parts: list[str] = [record.name]
    current = record

    while current.parent_id and not is_virtual(current.parent_id):
        parent = node_cache.get(current.parent_id)
        if parent is None:
            return None
        parts.append(parent.name)
        current = parent

    if not current.parent_id:
        return None
    mount = mounts.get(current.parent_id)
    if mount is None:
        return None

    relative = "/".join(reversed(parts))
    return mount.local_path(mount.source.rstrip("/") + "/" + relative)
