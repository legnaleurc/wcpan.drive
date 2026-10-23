"""Full traversal of the mounted directories into the mirror.

Each pass is one ``ReconcileSession``: listings report what they saw, folders
that cannot be listed keep their subtree, and nodes no listing reported are
checked by path before removal.

An entry keeps its node ID while the object it names is the same: same name and
identity under the same parent, or an identity whose previous path no longer
names it (a rename or move). Hard links share an identity, so a new link to an
object that is still in place gets its own node.
"""

import os
import stat
import uuid
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.lib import guess_mime_type
from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.lib.bfs import parallel_bfs
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID, mount_id, structural_records
from wcpan.drive.server.services.operations import LocateUndecidedError
from wcpan.drive.server.services.reconcile import ReconcileSession, reconcile_session
from wcpan.drive.server.services.storage import StorageService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation

from .blocking import Blocking
from .handles import filesystem_id
from .paths import NodePaths
from .reading import HASH_ALGORITHM, Entry, hash_file, list_directory, stat_entry
from .store import EntryState, FilesystemStore


_L = getLogger(__name__)

type _Folder = tuple[MirrorStableId, str]


@dataclass(frozen=True, slots=True)
class _Previous:
    record: NodeRecord
    state: EntryState


def _new_node_id() -> MirrorStableId:
    # Hex never starts with the virtual ID prefix.
    return MirrorStableId(uuid.uuid4().hex)


def _same_public_metadata(a: NodeRecord, b: NodeRecord) -> bool:
    """Equal apart from probed media dimensions, which scans do not produce."""
    return (
        a.parent_id,
        a.name,
        a.is_directory,
        a.created_time,
        a.modified_time,
        a.changed_time,
        a.mime_type,
        a.hash,
        a.hash_algorithm,
        a.size,
        a.is_image,
        a.is_video,
    ) == (
        b.parent_id,
        b.name,
        b.is_directory,
        b.created_time,
        b.modified_time,
        b.changed_time,
        b.mime_type,
        b.hash,
        b.hash_algorithm,
        b.size,
        b.is_image,
        b.is_video,
    )


def _record(
    entry: Entry, node_id: MirrorStableId, parent_id: MirrorStableId, hash_: str
) -> NodeRecord:
    st = entry.stat
    mime_type = guess_mime_type(entry.name, is_directory=entry.is_directory)
    return NodeRecord(
        id=node_id,
        parent_id=parent_id,
        name=entry.name,
        is_directory=entry.is_directory,
        # Linux exposes no birth time through os.stat.
        created_time=int(st.st_mtime),
        modified_time=int(st.st_mtime),
        changed_time=int(st.st_ctime),
        mime_type=mime_type,
        hash=hash_,
        hash_algorithm=HASH_ALGORITHM if hash_ else "",
        size=0 if entry.is_directory else st.st_size,
        is_image=mime_type.startswith("image/"),
        is_video=mime_type.startswith("video/"),
        width=0,
        height=0,
        ms_duration=0,
    )


class FilesystemScanner:
    def __init__(
        self,
        *,
        storage: StorageService,
        store: FilesystemStore,
        node_sync: NodeSyncService,
        paths: NodePaths,
        blocking: Blocking,
    ) -> None:
        self._storage = storage
        self._store = store
        self._node_sync = node_sync
        self._paths = paths
        self._blocking = blocking

    async def ensure_structure(self) -> None:
        """Create the root and mount nodes; feed rows only on the first run."""
        records = structural_records(self._paths.mounts)
        if await self._storage.get_node_by_id(SERVER_ROOT_ID) is None:
            async with self._node_sync.observe() as read:
                await self._node_sync.upsert_batch(
                    [NodeObservation(record=r, locator=None) for r in records],
                    read=read,
                )
        else:
            await self._node_sync.sync_nodes(records)

    async def scan(self) -> None:
        """One full pass over every mount."""
        mounts = self._paths.mounts
        async with reconcile_session(
            {mount_id(name) for name in mounts},
            storage=self._storage,
            node_sync=self._node_sync,
            locate=self.locate,
        ) as session:
            roots: list[_Folder] = []
            for name, root in mounts.items():
                if await self._source_present(name, root):
                    roots.append((mount_id(name), root))
                else:
                    session.preserve(mount_id(name))
            claimed = set[MirrorStableId]()
            await parallel_bfs(
                roots, partial(self._visit, session=session, claimed=claimed)
            )
            await session.commit_removals()

    async def locate(self, node_id: MirrorStableId) -> NodeObservation | None:
        """Current observation of *node_id* at its mirrored path, if still there."""
        path = await self._paths.path_of(node_id)
        record = await self._storage.get_node_by_id(node_id)
        state = (await self._store.get_entries([node_id])).get(node_id)
        if path is None or record is None or record.parent_id is None or state is None:
            return None
        try:
            entry = await self._blocking(stat_entry, path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as e:
            raise LocateUndecidedError(str(e)) from e
        if entry is None or entry.identity != state.identity:
            return None
        previous = _Previous(record, state)
        observation = await self._observe(entry, node_id, record.parent_id, previous)
        if observation is not None:
            return observation
        if entry.state == state:
            return NodeObservation(record=record, locator=state.to_locator())
        raise LocateUndecidedError(f"{path} is changing")

    async def _source_present(self, name: str, root: str) -> bool:
        """Whether *root* is the directory this mount was scanned from before.

        An unmounted source can leave an empty mount-point directory, which must
        not be mistaken for a source whose files were all deleted.
        """
        try:
            st = await self._blocking(os.stat, root)
            fsid = await self._blocking(filesystem_id, root)
        except OSError as e:
            _L.error("Mount %r source %s is unavailable: %s", name, root, e)
            return False
        if not stat.S_ISDIR(st.st_mode):
            _L.error("Mount %r source %s is not a directory", name, root)
            return False
        known = await self._store.get_mount_fsid(name, root)
        if known is None:
            await self._node_sync.enqueue(
                partial(self._store.set_mount_fsid, name, root, fsid)
            )
            return True
        if known != fsid:
            _L.error(
                "Mount %r source %s is on a different filesystem than before;"
                " keeping its mirror. Run `rebind-mount %s` if this is intended.",
                name,
                root,
                name,
            )
            return False
        return True

    async def _visit(
        self,
        folder: _Folder,
        *,
        session: ReconcileSession,
        claimed: set[MirrorStableId],
    ) -> list[_Folder]:
        folder_id, folder_path = folder
        async with self._node_sync.observe() as read:
            try:
                entries = await self._blocking(list_directory, folder_path)
            except OSError as e:
                _L.warning("Cannot list %s; keeping its mirror: %s", folder_path, e)
                session.preserve(folder_id)
                return []

            existing = await self._storage.get_children(folder_id)
            states = await self._store.get_entries([r.id for r in existing])
            by_name = {r.name: r for r in existing}

            directories: list[NodeObservation] = []
            files: list[NodeObservation] = []
            children: list[_Folder] = []
            for entry in entries:
                node_id, previous = await self._match(entry, by_name, states, claimed)
                session.saw([node_id])
                if entry.is_directory:
                    children.append((node_id, entry.path))
                observation = await self._observe(entry, node_id, folder_id, previous)
                if observation is None:
                    continue
                if entry.is_directory:
                    directories.append(observation)
                else:
                    files.append(observation)

            await self._node_sync.upsert_batch(directories, read=read)
            await self._node_sync.upsert_file_batch(files, read=read)
        return children

    async def _match(
        self,
        entry: Entry,
        by_name: dict[str, NodeRecord],
        states: dict[MirrorStableId, EntryState],
        claimed: set[MirrorStableId],
    ) -> tuple[MirrorStableId, _Previous | None]:
        """The node ID for *entry*, and what that node was last observed as."""
        same_name = by_name.get(entry.name)
        if same_name is not None and same_name.id not in claimed:
            state = states.get(same_name.id)
            if state is not None and state.identity == entry.identity:
                claimed.add(same_name.id)
                return same_name.id, _Previous(same_name, state)

        for candidate in await self._store.nodes_by_identity(entry.identity):
            if candidate in claimed:
                continue
            claimed.add(candidate)
            previous = await self._moved_from(candidate, entry)
            if previous is not None:
                return candidate, previous
            claimed.discard(candidate)

        node_id = _new_node_id()
        claimed.add(node_id)
        return node_id, None

    async def _moved_from(
        self, candidate: MirrorStableId, entry: Entry
    ) -> _Previous | None:
        """*candidate*'s last observation, if its old path no longer names *entry*."""
        record = await self._storage.get_node_by_id(candidate)
        state = (await self._store.get_entries([candidate])).get(candidate)
        if record is None or state is None or record.is_directory != entry.is_directory:
            return None
        path = await self._paths.path_of(candidate)
        if path is not None and path != entry.path:
            try:
                current = await self._blocking(stat_entry, path)
            except OSError:
                current = None
            if current is not None and current.identity == entry.identity:
                return None  # still in place: entry is another hard link
        return _Previous(record, state)

    async def _observe(
        self,
        entry: Entry,
        node_id: MirrorStableId,
        parent_id: MirrorStableId,
        previous: _Previous | None,
    ) -> NodeObservation | None:
        """An observation to apply, or ``None`` when nothing changed or it is withheld.

        File checksums are reused while the stat signature is unchanged. A file
        that changes while being hashed is withheld, keeping its last complete
        version visible until a later pass.
        """
        state = entry.state
        hash_ = ""
        if not entry.is_directory:
            if (
                previous is not None
                and previous.state == state
                and previous.record.hash
            ):
                hash_ = previous.record.hash
            else:
                try:
                    digest = await self._blocking(hash_file, entry.path, state)
                except OSError as e:
                    _L.warning("Cannot read %s; withholding it: %s", entry.path, e)
                    return None
                if digest is None:
                    _L.debug("%s changed while hashing; withholding it", entry.path)
                    return None
                hash_ = digest
        record = _record(entry, node_id, parent_id, hash_)
        if (
            previous is not None
            and previous.state == state
            and _same_public_metadata(previous.record, record)
        ):
            return None
        return NodeObservation(record=record, locator=state.to_locator())
