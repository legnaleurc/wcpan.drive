"""Server-internal types (not part of client / shared lib surface)."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NewType

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
class ServerConfig:
    """Backend-neutral server settings; the backend parses its own section."""

    host: str
    port: int
    database_url: str
    # Mount label to backend source path, interpreted by the backend.
    mounts: dict[str, str]
    local_paths: dict[str, str]
    log_path: str | None = None
    upload_tmp_dir: str | None = None
