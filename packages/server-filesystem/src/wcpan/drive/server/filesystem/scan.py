"""Traversal of the mounted directories into the mirror.

Each pass is one ``ReconcileSession``: listings report what they saw, folders
that cannot be listed keep their subtree, and nodes no listing reported are
checked by path before removal. A full pass lists every folder; a scoped pass
lists only folders reported as changed, plus new folders whose contents the
mirror has not seen. Passes run one at a time, so two listings of the same new
entry cannot create two nodes for it.

An entry keeps its node ID while the object it names is the same: same name and
identity under the same parent, or an identity whose previous path no longer
names it (a rename or move). Hard links share an identity, so a new link to an
object that is still in place gets its own node.
"""

import asyncio
import os
import stat
import uuid
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from wcpan.drive.protocol.lib import guess_mime_type
from wcpan.drive.protocol.types import MirrorStableId, NodeRecord
from wcpan.drive.server.lib.bfs import parallel_bfs
from wcpan.drive.server.lib.mounts import mount_id, mount_name
from wcpan.drive.server.services.operations import LocateUndecidedError
from wcpan.drive.server.services.reconcile import ReconcileSession, reconcile_session
from wcpan.drive.server.services.storage import StorageService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.types import NodeObservation

from .blocking import Blocking
from .handles import filesystem_id
from .paths import NodePaths
from .reading import (
    HASH_ALGORITHM,
    Entry,
    hash_file,
    list_directory,
    same_content,
    stat_entry,
)
from .store import EntryState, FilesystemStore
from .watch import DirectoryWatcher


_L = getLogger(__name__)

type _Folder = tuple[MirrorStableId, str]


@dataclass(frozen=True, slots=True)
class Previous:
    """What a node was last observed as."""

    record: NodeRecord
    state: EntryState


def new_node_id() -> MirrorStableId:
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
        watcher: DirectoryWatcher | None = None,
    ) -> None:
        self._storage = storage
        self._store = store
        self._node_sync = node_sync
        self._paths = paths
        self._blocking = blocking
        self._watcher = watcher
        self._pass_lock = asyncio.Lock()

    async def scan(self) -> None:
        """One full pass over every mount."""
        mounts = {mount_id(name) for name in self._paths.mounts}
        await self._pass(mounts, scoped=None)

    async def scan_folders(self, folder_ids: Iterable[MirrorStableId]) -> None:
        """Re-read *folder_ids* only; their unchanged subfolders keep their mirror.

        A folder that is gone or now names another object is replaced by its
        parent, whose listing decides whether it was removed, moved, or replaced.
        """
        async with self._pass_lock:
            scope = await self._present_folders(folder_ids)
        if scope:
            await self._pass(scope, scoped=scope)

    async def _present_folders(
        self, folder_ids: Iterable[MirrorStableId]
    ) -> set[MirrorStableId]:
        present = set[MirrorStableId]()
        pending = list(folder_ids)
        visited = set[MirrorStableId]()
        while pending:
            folder_id = pending.pop()
            if folder_id in visited:
                continue
            visited.add(folder_id)
            if mount_name(folder_id) is not None or await self._still_present(
                folder_id
            ):
                present.add(folder_id)
                continue
            record = await self._storage.get_node_by_id(folder_id)
            if record is not None and record.parent_id is not None:
                pending.append(record.parent_id)
        return present

    async def _still_present(self, folder_id: MirrorStableId) -> bool:
        """Whether the folder's mirrored path still names the same directory."""
        path = await self._paths.path_of(folder_id)
        state = (await self._store.get_entries([folder_id])).get(folder_id)
        if path is None or state is None:
            return False
        try:
            entry = await self._blocking(stat_entry, path)
        except OSError:
            return False
        return (
            entry is not None
            and entry.is_directory
            and entry.identity == state.identity
        )

    async def _pass(
        self, scope: set[MirrorStableId], *, scoped: set[MirrorStableId] | None
    ) -> None:
        async with self._pass_lock:
            async with reconcile_session(
                scope,
                storage=self._storage,
                node_sync=self._node_sync,
                locate=self.locate,
            ) as session:
                roots: list[_Folder] = []
                for folder_id in sorted(scope):
                    path = await self._folder_path(folder_id)
                    if path is None:
                        session.preserve(folder_id)
                    else:
                        roots.append((folder_id, path))
                claimed = set[MirrorStableId]()
                await parallel_bfs(
                    roots,
                    partial(
                        self._visit, session=session, claimed=claimed, scoped=scoped
                    ),
                )
                await session.commit_removals()

    @asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[None, None]:
        """Hold off passes while a write changes the tree and records it.

        Otherwise a pass could list the new entry first and give it another ID.
        """
        async with self._pass_lock:
            yield

    async def current(self, node_id: MirrorStableId) -> tuple[Entry, Previous] | None:
        """*node_id* at its mirrored path, if that path still names the same object."""
        path = await self._paths.path_of(node_id)
        record = await self._storage.get_node_by_id(node_id)
        state = (await self._store.get_entries([node_id])).get(node_id)
        if path is None or record is None or state is None:
            return None
        try:
            entry = await self._blocking(stat_entry, path)
        except OSError:
            return None
        if entry is None or entry.identity != state.identity:
            return None
        return entry, Previous(record, state)

    async def checksum(self, entry: Entry, previous: Previous | None) -> str | None:
        """The file checksum, reused from *previous* while content is unchanged.

        ``""`` for directories; ``None`` when the file changed while hashed.
        """
        if entry.is_directory:
            return ""
        if (
            previous is not None
            and previous.record.hash
            and same_content(previous.state, entry.state)
        ):
            return previous.record.hash
        return await self._blocking(hash_file, entry.path, entry.state)

    @staticmethod
    def observation(
        entry: Entry, node_id: MirrorStableId, parent_id: MirrorStableId, hash_: str
    ) -> NodeObservation:
        return NodeObservation(
            record=_record(entry, node_id, parent_id, hash_),
            locator=entry.state.to_locator(),
        )

    async def folder_path(self, folder_id: MirrorStableId) -> str | None:
        """The directory of a mount or mirrored folder, or ``None`` if unusable."""
        return await self._folder_path(folder_id)

    async def _folder_path(self, folder_id: MirrorStableId) -> str | None:
        """The directory to list, or ``None`` when it cannot be read safely."""
        name = mount_name(folder_id)
        if name is not None:
            root = self._paths.mounts.get(name)
            if root is None or not await self._source_present(name, root):
                return None
            return root
        return await self._paths.path_of(folder_id)

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
        previous = Previous(record, state)
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
                " keeping its mirror. If intended, run `backend <instance>"
                " rebind-mount %s`.",
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
        scoped: set[MirrorStableId] | None,
    ) -> list[_Folder]:
        """List one folder; return the subfolders this pass should list too.

        In a scoped pass, only new subfolders are listed; others in the scope are
        listed on their own, and the rest keep their mirrored contents.
        """
        folder_id, folder_path = folder
        if self._watcher is not None:
            # Watch before listing, so no change after the listing goes unseen.
            self._watcher.watch(folder_id, folder_path)
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
                    if scoped is None or previous is None:
                        children.append((node_id, entry.path))
                    elif node_id not in scoped:
                        session.preserve(node_id)
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
    ) -> tuple[MirrorStableId, Previous | None]:
        """The node ID for *entry*, and what that node was last observed as."""
        same_name = by_name.get(entry.name)
        if same_name is not None and same_name.id not in claimed:
            state = states.get(same_name.id)
            if state is not None and state.identity == entry.identity:
                claimed.add(same_name.id)
                return same_name.id, Previous(same_name, state)

        for candidate in await self._store.nodes_by_identity(entry.identity):
            if candidate in claimed:
                continue
            # The check awaits the disk; the candidate's own folder may claim it
            # by name meanwhile, which takes precedence over a move.
            previous = await self._moved_from(candidate, entry)
            if previous is not None and candidate not in claimed:
                claimed.add(candidate)
                return candidate, previous

        node_id = new_node_id()
        claimed.add(node_id)
        return node_id, None

    async def _moved_from(
        self, candidate: MirrorStableId, entry: Entry
    ) -> Previous | None:
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
        return Previous(record, state)

    async def _observe(
        self,
        entry: Entry,
        node_id: MirrorStableId,
        parent_id: MirrorStableId,
        previous: Previous | None,
    ) -> NodeObservation | None:
        """An observation to apply, or ``None`` when nothing changed or it is withheld.

        File checksums are reused while the content is unchanged. A file that
        changes while being hashed is withheld, keeping its last complete version
        visible until a later pass.
        """
        state = entry.state
        try:
            hash_ = await self.checksum(entry, previous)
        except OSError as e:
            _L.warning("Cannot read %s; withholding it: %s", entry.path, e)
            return None
        if hash_ is None:
            _L.debug("%s changed while hashing; withholding it", entry.path)
            return None
        record = _record(entry, node_id, parent_id, hash_)
        if (
            previous is not None
            and previous.state == state
            and _same_public_metadata(previous.record, record)
        ):
            return None
        return NodeObservation(record=record, locator=state.to_locator())
