"""Server-internal types (not part of client / shared lib surface)."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NewType

from wcpan.drive.protocol.types import NodeRecord
from wcpan.synology import SynologyPath


# ---------------------------------------------------------------------------
# Path-space type aliases
# ---------------------------------------------------------------------------

VirtualPath = NewType("VirtualPath", PurePosixPath)
"""Client-facing virtual path, e.g. /photos/2024."""


SynologyLocator = NewType("SynologyLocator", str)
"""Synology ``file_id``; mutable across some operations, unlike the public node ID."""


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
    locator: SynologyLocator | None


# ---------------------------------------------------------------------------
# Worker / queue types
# ---------------------------------------------------------------------------

type WriteQueue = asyncio.Queue[Callable[[], Awaitable[None]]]


@dataclass(frozen=True, slots=True)
class MetadataWorkItem:
    """One async media-enrichment job; consumed by ``metadata_worker``."""

    observation: NodeObservation
    force_refresh: bool
    # Token from ``NodeSyncService``; the write is dropped once a newer write claims the node.
    claim: object


type MetadataQueue = asyncio.Queue[MetadataWorkItem]


@dataclass(frozen=True, kw_only=True)
class ServerConfig:
    host: str
    port: int
    database_url: str
    synology_url: str
    username: str
    password: str
    mounts: dict[str, SynologyPath]
    public_url: str
    webhook_app_id: str
    local_paths: dict[str, str]
    otp_code: str | None = None
    log_path: str | None = None
    upload_tmp_dir: str | None = None
