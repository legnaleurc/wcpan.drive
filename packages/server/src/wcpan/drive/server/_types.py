"""Server-internal types (not part of client / shared lib surface)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, NewType

from wcpan.drive.protocol.types import NodeRecord


# ---------------------------------------------------------------------------
# Path-space type aliases
# ---------------------------------------------------------------------------

VirtualPath = NewType("VirtualPath", PurePosixPath)
"""Client-facing virtual path, e.g. /photos/2024."""


ReadSeq = NewType("ReadSeq", int)
"""Sequence number taken when a backend read starts (see ``_services.sync.ordering``)."""

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
    # The backend's key for this item in its source, naming it in media failure
    # records: Synology's permanent link, the filesystem's source path. A file
    # without one is not recorded when its media probe fails.
    source_key: str | None = None


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


class ProbeFailure(StrEnum):
    """Why a probe did not produce dimensions; decides when to retry."""

    # Read errors and timeouts, a file changing mid-probe, MediaInfo failing or
    # crashing, the deadline passing while waiting for the source: retry with
    # backoff.
    TRANSIENT = "transient"
    # The parser rejects the bytes, or reports a zero width or height: wait for
    # a content change.
    INVALID = "invalid"
    # The deadline passed while MediaInfo was parsing: inconclusive, not invalid.
    LIMIT = "limit"
