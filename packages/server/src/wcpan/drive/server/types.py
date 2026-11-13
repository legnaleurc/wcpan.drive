"""Server-internal types (not part of client / shared lib surface)."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NewType

from wcpan.drive.protocol.types import NodeRecord


# ---------------------------------------------------------------------------
# Path-space type aliases
# ---------------------------------------------------------------------------

VirtualPath = NewType("VirtualPath", PurePosixPath)
"""Client-facing virtual path, e.g. /photos/2024."""


ReadSeq = NewType("ReadSeq", int)
"""Sequence number taken when a backend read starts (see ``services.ordering``)."""

BackendLocator = NewType("BackendLocator", str)
"""Backend-private reference to a node, opaque to the core (see ``BackendSchema``)."""


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class NodeObservation:
    """Public node metadata read from the backend, with its private locator.

    ``locator`` is persisted in the same transaction as ``record`` but is not part
    of the public node. ``None`` keeps any existing binding unchanged.
    """

    record: NodeRecord
    locator: BackendLocator | None
    # Backend instance that made the observation; set by its ``NodeSyncService``
    # so the mirror binds the locator with that backend's schema. None only for
    # the core's own nodes (root and mounts), which carry no locator.
    backend: str | None = None


# ---------------------------------------------------------------------------
# Worker / queue types
# ---------------------------------------------------------------------------

type WriteQueue = asyncio.Queue[Callable[[], Awaitable[None]]]


@dataclass(frozen=True, slots=True)
class MetadataWorkItem:
    """One async media-enrichment job; consumed by ``metadata_worker``."""

    observation: NodeObservation
    force_refresh: bool
    # Start of the backend read that produced the observation.
    read: ReadSeq


type MetadataQueue = asyncio.Queue[MetadataWorkItem]


@dataclass(frozen=True, kw_only=True)
class MountConfig:
    # Name of the backend instance that serves this mount.
    backend: str
    # Source location, interpreted by that backend (a Synology path, a directory).
    source: str


@dataclass(frozen=True, kw_only=True)
class BackendConfig:
    # Import path of the backend factory (``module.function``).
    type: str
    # The rest of the backend's section, parsed by the backend.
    settings: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class ServerConfig:
    """Generic server settings plus the backend instances and mounts they serve."""

    host: str
    port: int
    database_url: str
    backends: dict[str, BackendConfig]
    mounts: dict[str, MountConfig]
    log_path: str | None = None

    def mounts_of(self, backend: str) -> dict[str, str]:
        """Mount name to source for the mounts *backend* serves."""
        return {
            name: mount.source
            for name, mount in self.mounts.items()
            if mount.backend == backend
        }
