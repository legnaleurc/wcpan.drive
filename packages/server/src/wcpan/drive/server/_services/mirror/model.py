"""Types, errors, and the instance registry shape shared by the mirror's modules."""

import secrets
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from wcpan.drive.protocol.types import NodeId

from ..._types import BackendLocator
from ..media.probe.source import ProbeFailure


@dataclass(frozen=True, slots=True)
class MediaFailure:
    """A file withheld after its media probe failed, and why it failed."""

    source_key: str
    kind: ProbeFailure


class SchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendNamespace:
    """The rows of one backend instance in the shared backend tables.

    ``key`` is the instance's registry key; backend tables store it in their
    ``instance`` column, and every lookup filters by it.
    """

    key: int


class BackendSchema(Protocol):
    """Backend-owned tables, maintained on the core's connection and transaction.

    The tables are shared by every instance of the backend. Each has an
    ``instance`` column holding the instance key (``BackendNamespace.key``), which
    every query filters by, so an instance never sees another's rows; ``drop``
    and ``rebuild`` delete an instance's rows through that column without loading
    the backend. Rows stay keyed by public node ID. Hooks must not commit, roll
    back, or open connections.
    """

    @property
    def name(self) -> str:
        """Names the set of tables; its version is recorded under this name."""
        ...

    @property
    def version(self) -> int:
        """Version of the tables; a database with another version is refused."""
        ...

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        """Create the tables, shared by every instance."""
        ...

    def bind_locator(
        self,
        con: sqlite3.Connection,
        ns: BackendNamespace,
        node_id: NodeId,
        locator: BackendLocator,
    ) -> None:
        """Persist the locator of *node_id* alongside its observation."""
        ...

    def forget_nodes(
        self, con: sqlite3.Connection, ns: BackendNamespace, node_ids: Sequence[str]
    ) -> None:
        """Drop backend state of removed nodes."""
        ...

    def forget_orphans(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        """Drop backend state of nodes no longer in ``nodes``."""
        ...


class MirrorNotReadyError(ValueError):
    """A read-only command found the database needs a write first.

    The message says what makes that write: the server's ``checkpoint``
    command, or starting the server once.
    """


@dataclass(frozen=True)
class Instance:
    """A registered backend instance; *schema* is set once its factory registers it."""

    ns: BackendNamespace
    schema: BackendSchema | None = None


type Instances = Mapping[str, Instance]
"""Registered backend instances by name."""


def with_schema(instances: Instances) -> list[Instance]:
    return [i for i in instances.values() if i.schema is not None]


def registered(instances: Instances, backend: str) -> Instance:
    instance = instances.get(backend)
    if instance is None:
        raise ValueError(f"Backend instance {backend!r} is not registered")
    return instance


def new_epoch() -> str:
    return secrets.token_hex(8)


def now_ms() -> int:
    return time.time_ns() // 1_000_000
