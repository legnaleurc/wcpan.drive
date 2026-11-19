"""``MirrorStore``: async access to the mirror, running its SQLite work off the main thread."""

import logging
import sqlite3
from collections.abc import Callable, Mapping
from functools import partial
from typing import Concatenate, Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord
from wcpan.drive.sqlite import (
    read_only as _read_only,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from ..._types import NodeObservation, ProbeFailure
from ..off_main import OffMainService
from .instances import (
    check_backend_schema,
    lookup_instance,
    owner_of,
    register_instance,
    register_schema,
    registered_instances,
)
from .maintenance import (
    cleanup_dangling_nodes,
    release_reassigned_mounts,
    reset_change_history,
)
from .media_failures import (
    clear_media_failure,
    get_media_failures,
    mark_media_failure,
    prune_media_failures,
)
from .model import (
    BackendNamespace,
    BackendSchema,
    Instance,
    MediaFailure,
    MirrorNotReadyError,
    registered,
)
from .reads import (
    MAX_CHANGES_PER_PAGE,
    build_deferred_preserved_set,
    collect_subtree_node_ids,
    deferred_removal_roots,
    get_ancestors,
    get_changes_since,
    get_children,
    get_cursor,
    get_node_by_id,
    get_nodes_by_ids,
    get_root,
    resolve_path_to_id,
)
from .schema import check_schema, ensure_schema
from .writes import (
    apply_observations,
    bulk_upsert_nodes,
    delete_subtree_and_emit_changes,
    upsert_node_and_emit_change,
)


_L = logging.getLogger(__name__)


def _run_read[R](
    dsn: str,
    fn: Callable[..., R],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> R:
    with _read_only(dsn) as con:
        return fn(con, *args, **kwargs)


def _run_write[R](
    dsn: str,
    fn: Callable[..., R],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> R:
    with _read_write(dsn) as con:
        return fn(con, *args, **kwargs)


class MirrorReader(Protocol):
    """The read side of ``MirrorStore``.

    What read-only maintenance commands get (``ReadCoreServices``), so code
    written against it cannot write the mirror. ``MirrorStore`` implements it.
    """

    async def read[**A, R](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], R],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> R: ...

    async def owner_of(self, node_id: NodeId) -> str | None: ...

    async def get_media_failures(self, backend: str) -> list[MediaFailure]: ...

    async def resolve_path_to_id(self, segments: list[str]) -> NodeId | None: ...

    async def get_root(self) -> NodeRecord | None: ...

    async def get_node_by_id(self, node_id: NodeId) -> NodeRecord | None: ...

    async def get_nodes_by_ids(
        self, node_ids: list[NodeId]
    ) -> dict[NodeId, NodeRecord]: ...

    async def get_children(self, parent_id: NodeId) -> list[NodeRecord]: ...

    async def get_ancestors(self, node_id: NodeId) -> list[NodeRecord]: ...

    async def build_deferred_preserved_set(
        self, seen_ids: set[NodeId], preserve_roots: set[NodeId]
    ) -> set[NodeId]: ...

    async def deferred_removal_roots(
        self, preserved: set[NodeId], scope: set[NodeId]
    ) -> list[NodeId]: ...


class MirrorStore:
    """Async SQLite access - delegates blocking calls to OffMainService."""

    def __init__(
        self,
        dsn: str,
        *,
        off_main: OffMainService,
        schemas: Mapping[str, BackendSchema] | None = None,
    ) -> None:
        """*schemas* are registered by ``ensure_schema``, for instances that
        are not entered through a factory."""
        self._dsn = dsn
        self._off_main = off_main
        self._initial_schemas = dict(schemas or {})
        self._instances: dict[str, Instance] = {}
        self._epoch = ""

    @property
    def epoch(self) -> str:
        """The change history's epoch, read by ``ensure_schema``.

        Only offline maintenance (``squash``) replaces it, so it stays fixed
        while the server runs.
        """
        return self._epoch

    async def ensure_schema(self) -> None:
        self._epoch = await self._off_main(ensure_schema, self._dsn)
        for backend, schema in self._initial_schemas.items():
            await self.register_schema(backend, schema)

    async def check_schema(self) -> None:
        """Check the core schema without changing it, for read-only commands.

        Raises:
            SchemaVersionError: For another schema version.
            MirrorNotReadyError: If the database needs a write first.
        """
        self._epoch = await self._off_main(check_schema, self._dsn)

    async def lookup_instance(self, backend: str) -> int:
        """The registry key of instance *backend*, without registering it.

        Raises:
            MirrorNotReadyError: If the server has not registered it yet.
        """
        key = await self._off_main(lookup_instance, self._dsn, backend)
        if key is None:
            raise MirrorNotReadyError(
                f"backend {backend!r} is not registered; start the server once"
                " to set it up"
            )
        return key

    async def check_backend_schema(
        self, backend: str, schema: BackendSchema
    ) -> BackendNamespace:
        """The namespace of registered instance *backend*, checked against
        *schema*; ``register_schema`` without writing.

        Raises:
            MirrorNotReadyError: If the instance or its tables are not set up.
            SchemaVersionError: As ``register_schema``.
        """
        key = await self._off_main(check_backend_schema, self._dsn, backend, schema)
        ns = BackendNamespace(key)
        self._instances[backend] = Instance(ns=ns, schema=schema)
        return ns

    async def register_instance(self, backend: str) -> int:
        """Register instance *backend*; returns its key."""
        instance = self._instances.get(backend)
        if instance is not None:
            return instance.ns.key
        key = await self._off_main(register_instance, self._dsn, backend)
        self._instances[backend] = Instance(ns=BackendNamespace(key))
        return key

    async def register_schema(
        self, backend: str, schema: BackendSchema
    ) -> BackendNamespace:
        """Persist instance *backend*'s state through *schema* from now on.

        Returns the namespace that scopes the instance's rows.

        Raises:
            SchemaVersionError: If the backend's tables have another version,
                or *backend* holds rows of another backend's tables.
        """
        await self.register_instance(backend)
        ns = self._instances[backend].ns
        await self._off_main(register_schema, self._dsn, backend, ns, schema)
        self._instances[backend] = Instance(ns=ns, schema=schema)
        return ns

    def namespace(self, backend: str) -> BackendNamespace:
        """The namespace of registered instance *backend*."""
        return self._instances[backend].ns

    async def registered_instances(self) -> list[str]:
        """Names of every instance this database has seen, configured or not."""
        return await self._off_main(registered_instances, self._dsn)

    async def release_reassigned_mounts(self, mounts: Mapping[NodeId, str]) -> int:
        """Remove what other instances left under mounts now served by another.

        *mounts* maps mount node IDs to their configured instance. Run before
        backends start, so the new owner discovers the mount from scratch.
        Returns the number of subtrees removed.
        """
        return await self._off_main(
            release_reassigned_mounts, self._dsn, mounts, self._instances
        )

    async def owner_of(self, node_id: NodeId) -> str | None:
        """The instance whose node *node_id* is; ``None`` for core nodes."""
        return await self._off_main(owner_of, self._dsn, node_id)

    async def read[**A, R](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], R],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> R:
        """Run *fn* on a read-only connection; for backend-owned queries."""
        return await self._off_main(_run_read, self._dsn, fn, args, kwargs)

    async def write[**A, R](
        self,
        fn: Callable[Concatenate[sqlite3.Connection, A], R],
        /,
        *args: A.args,
        **kwargs: A.kwargs,
    ) -> R:
        """Run *fn* in one write transaction; call it from the write queue."""
        return await self._off_main(_run_write, self._dsn, fn, args, kwargs)

    async def bulk_upsert_nodes(self, records: list[NodeRecord]) -> None:
        await self._off_main(bulk_upsert_nodes, self._dsn, records)

    async def upsert_node_and_emit_change(self, record: NodeRecord) -> None:
        await self._off_main(upsert_node_and_emit_change, self._dsn, record)

    async def delete_subtree_and_emit_changes(self, node_id: NodeId) -> None:
        await self._off_main(
            delete_subtree_and_emit_changes, self._dsn, node_id, self._instances
        )

    async def apply_observations(
        self, observations: list[NodeObservation]
    ) -> set[NodeId]:
        return await self._off_main(
            apply_observations, self._dsn, observations, self._instances
        )

    def _key(self, backend: str) -> int:
        return registered(self._instances, backend).ns.key

    async def mark_media_failure(
        self, backend: str, source_key: str, kind: ProbeFailure
    ) -> None:
        """Record that the media probe of *source_key* in *backend* failed.

        Raises:
            ValueError: If *backend* is not registered.
        """
        await self._off_main(
            mark_media_failure, self._dsn, self._key(backend), source_key, kind
        )

    async def clear_media_failure(
        self, backend: str, source_key: str, *, before: int
    ) -> bool:
        """Forget the failure record of *source_key*, whose file is gone.

        Only a record last marked before *before* (ms) is removed, so a failure
        recorded after the file was found gone is kept. Returns whether one was.

        Raises:
            ValueError: If *backend* is not registered.
        """
        return await self._off_main(
            clear_media_failure, self._dsn, self._key(backend), source_key, before
        )

    async def prune_media_failures(self, backend: str, *, before: int) -> int:
        """Forget *backend*'s failure records last marked before *before* (ms).

        Returns how many were removed.

        Raises:
            ValueError: If *backend* is not registered.
        """
        return await self._off_main(
            prune_media_failures, self._dsn, self._key(backend), before
        )

    async def get_media_failures(self, backend: str) -> list[MediaFailure]:
        """*backend*'s failure records by source key; none if it is not registered."""
        instance = self._instances.get(backend)
        if instance is None:
            return []
        return await self._off_main(get_media_failures, self._dsn, instance.ns.key)

    async def resolve_path_to_id(self, segments: list[str]) -> NodeId | None:
        return await self._off_main(resolve_path_to_id, self._dsn, segments)

    async def get_root(self) -> NodeRecord | None:
        """The server root: the only node without a parent."""
        return await self._off_main(get_root, self._dsn)

    async def get_node_by_id(self, node_id: NodeId) -> NodeRecord | None:
        return await self._off_main(get_node_by_id, self._dsn, node_id)

    async def get_nodes_by_ids(
        self,
        node_ids: list[NodeId],
    ) -> dict[NodeId, NodeRecord]:
        return await self._off_main(get_nodes_by_ids, self._dsn, node_ids)

    async def get_children(self, parent_id: NodeId) -> list[NodeRecord]:
        return await self._off_main(get_children, self._dsn, parent_id)

    async def get_ancestors(self, node_id: NodeId) -> list[NodeRecord]:
        return await self._off_main(get_ancestors, self._dsn, node_id)

    async def collect_subtree_node_ids(
        self,
        root_id: NodeId,
    ) -> set[NodeId]:
        return await self._off_main(collect_subtree_node_ids, self._dsn, root_id)

    async def build_deferred_preserved_set(
        self,
        seen_ids: set[NodeId],
        preserve_roots: set[NodeId],
    ) -> set[NodeId]:
        return await self._off_main(
            build_deferred_preserved_set, self._dsn, seen_ids, preserve_roots
        )

    async def deferred_removal_roots(
        self,
        preserved: set[NodeId],
        scope: set[NodeId],
    ) -> list[NodeId]:
        return await self._off_main(deferred_removal_roots, self._dsn, preserved, scope)

    async def get_cursor(self) -> int:
        return await self._off_main(get_cursor, self._dsn)

    async def get_changes_since(
        self,
        cursor: int,
        max_size: int = MAX_CHANGES_PER_PAGE,
    ) -> tuple[list[tuple[NodeId, bool, NodeRecord | None]], int, bool]:
        return await self._off_main(get_changes_since, self._dsn, cursor, max_size)

    async def cleanup_dangling_nodes(
        self, *, mounts: Mapping[str, str] | None = None
    ) -> int:
        return await self._off_main(
            partial(cleanup_dangling_nodes, mounts=mounts), self._dsn
        )

    async def reset_change_history(self) -> int:
        return await self._off_main(reset_change_history, self._dsn)


async def create_mirror_store(
    database_url: str,
    *,
    off_main: OffMainService,
    schemas: Mapping[str, BackendSchema] | None = None,
) -> MirrorStore:
    mirror = MirrorStore(database_url, off_main=off_main, schemas=schemas)
    _L.info("initializing database: %s", database_url)
    await mirror.ensure_schema()
    return mirror
