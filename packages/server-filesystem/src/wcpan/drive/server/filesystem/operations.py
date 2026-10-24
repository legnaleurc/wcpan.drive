"""Node operations and uploads for the filesystem backend.

Every change runs while scan passes are held off, and is recorded in the mirror
before they resume, so a pass that lists the result matches it to the same node.
Renames never replace an existing entry, and an entry is only renamed or deleted
while its path still names the object the mirror knows.
"""

import errno
import os
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from logging import getLogger
from typing import Any, BinaryIO

from wcpan.drive.protocol.types import MediaInfo, MirrorStableId, NodeRecord
from wcpan.drive.server.lib.mounts import SERVER_ROOT_ID
from wcpan.drive.server.services.operations import (
    OperationConflictError,
    OperationInvalidError,
    OperationNotSupportedError,
    OperationUnavailableError,
)
from wcpan.drive.server.services.storage import StorageService
from wcpan.drive.server.services.sync import NodeSyncService
from wcpan.drive.server.services.upload import (
    UploadConflictError,
    UploadNameTooLongError,
    UploadPermanentError,
    UploadTransientError,
)
from wcpan.drive.server.types import NodeObservation, ReadSeq

from .blocking import Blocking
from .mutation import (
    create_staging,
    discard_staging,
    finish_staging,
    make_directory,
    remove,
    rename_no_replace,
)
from .paths import NodePaths
from .reading import STAGING_PREFIX, Entry, new_digest, open_verified, stat_entry
from .scan import FilesystemScanner, new_node_id
from .store import FilesystemStore


_L = getLogger(__name__)

_CHUNK_SIZE = 1024 * 1024
_READ_ONLY = "The filesystem backend is read-only"
_DENIED = {errno.EACCES, errno.EPERM, errno.EROFS}
_NO_SPACE = {errno.ENOSPC, errno.EDQUOT}


def _check_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\0" in name
        or name.startswith(STAGING_PREFIX)
    ):
        raise OperationInvalidError(f"Invalid name {name!r}")


def _translate(e: OSError) -> Exception:
    if e.errno in _DENIED:
        return OperationNotSupportedError(f"Permission denied: {e.strerror}")
    if e.errno == errno.ENAMETOOLONG:
        return OperationInvalidError("Name is too long")
    if e.errno == errno.EINVAL:
        return OperationInvalidError(str(e))
    if e.errno == errno.EXDEV:
        return OperationUnavailableError("Cannot move across filesystems")
    return OperationUnavailableError(str(e))


class _Writes:
    """Shared plumbing for operations and uploads."""

    def __init__(
        self,
        *,
        scanner: FilesystemScanner,
        storage: StorageService,
        node_sync: NodeSyncService,
        blocking: Blocking,
        read_only: bool,
    ) -> None:
        self._scanner = scanner
        self._storage = storage
        self._node_sync = node_sync
        self._blocking = blocking
        self._read_only = read_only

    def _check_writable(self) -> None:
        if self._read_only:
            raise OperationNotSupportedError(_READ_ONLY)

    async def _folder(self, folder_id: MirrorStableId) -> str:
        if folder_id == SERVER_ROOT_ID:
            raise OperationNotSupportedError("Cannot change the server root")
        path = await self._scanner.folder_path(folder_id)
        if path is None:
            raise OperationUnavailableError(f"Folder {folder_id} is not available")
        return path

    async def _entry(self, path: str) -> Entry:
        try:
            entry = await self._blocking(stat_entry, path)
        except OSError as e:
            raise _translate(e) from e
        if entry is None:
            raise OperationUnavailableError(f"{path} is not a file or directory")
        return entry

    async def _mirrored_child(
        self, parent_id: MirrorStableId, name: str
    ) -> NodeRecord | None:
        """The node an existing entry maps to, reading its folder first."""
        await self._scanner.scan_folders([parent_id])
        await self._node_sync.wait_enrichment_drained()
        children = await self._storage.get_children(parent_id)
        return next((c for c in children if c.name == name), None)

    async def _record(self, observation: NodeObservation, read: ReadSeq) -> NodeRecord:
        record = await self._node_sync.upsert(observation, read=read)
        # Recorded before passes resume, so they match it instead of minting an ID.
        await self._node_sync.flush()
        return record


class FilesystemOperations(_Writes):
    def __init__(
        self,
        *,
        scanner: FilesystemScanner,
        storage: StorageService,
        node_sync: NodeSyncService,
        paths: NodePaths,
        store: FilesystemStore,
        blocking: Blocking,
        read_only: bool = False,
    ) -> None:
        super().__init__(
            scanner=scanner,
            storage=storage,
            node_sync=node_sync,
            blocking=blocking,
            read_only=read_only,
        )
        self._paths = paths
        self._store = store

    async def create_folder(self, parent_id: MirrorStableId, name: str) -> NodeRecord:
        self._check_writable()
        _check_name(name)
        async with self._scanner.exclusive():
            folder = await self._folder(parent_id)
            path = os.path.join(folder, name)
            async with self._node_sync.observe() as read:
                try:
                    await self._blocking(make_directory, path)
                except FileExistsError:
                    pass
                except OSError as e:
                    raise _translate(e) from e
                else:
                    entry = await self._entry(path)
                    observation = FilesystemScanner.observation(
                        entry, new_node_id(), parent_id, ""
                    )
                    return await self._record(observation, read)
        raise OperationConflictError(
            f"{name!r} already exists in this folder",
            record=await self._mirrored_child(parent_id, name),
        )

    async def update(
        self,
        record: NodeRecord,
        *,
        name: str | None,
        parent_id: MirrorStableId | None,
    ) -> NodeRecord:
        self._check_writable()
        new_name = name if name and name != record.name else None
        new_parent = parent_id if parent_id and parent_id != record.parent_id else None
        if new_name is None and new_parent is None:
            return record
        if new_name is not None:
            _check_name(new_name)
        async with self._scanner.exclusive():
            current = await self._scanner.current(record.id)
            if current is None:
                raise OperationUnavailableError(
                    f"{record.name!r} changed on disk; retry after the next scan"
                )
            entry, previous = current
            target = new_parent or previous.record.parent_id
            assert target is not None
            folder = await self._folder(target)
            destination = os.path.join(folder, new_name or entry.name)
            async with self._node_sync.observe() as read:
                try:
                    await self._blocking(rename_no_replace, entry.path, destination)
                except FileExistsError as e:
                    raise OperationConflictError(
                        f"{new_name or entry.name!r} already exists in this folder"
                    ) from e
                except OSError as e:
                    raise _translate(e) from e
                moved = await self._entry(destination)
                hash_ = await self._scanner.checksum(moved, previous)
                observation = FilesystemScanner.observation(
                    moved, record.id, target, hash_ or previous.record.hash
                )
                return await self._record(observation, read)

    async def delete(self, record: NodeRecord) -> None:
        """Delete permanently; a directory is removed with its contents."""
        self._check_writable()
        failure: OSError | None = None
        async with self._scanner.exclusive():
            current = await self._scanner.current(record.id)
            if current is None and await self._still_exists(record.id):
                raise OperationUnavailableError(
                    f"{record.name!r} changed on disk; retry after the next scan"
                )
            async with self._node_sync.observe() as read:
                if current is not None:
                    entry, _ = current
                    try:
                        await self._blocking(
                            remove, entry.path, is_directory=entry.is_directory
                        )
                    except FileNotFoundError:
                        pass
                    except OSError as e:
                        failure = e
                if failure is None:
                    await self._node_sync.delete(record.id, read=read)
                    await self._node_sync.flush()
                    return
        # Part of a directory may be gone; record what is left.
        folders = [record.id] + ([record.parent_id] if record.parent_id else [])
        await self._scanner.scan_folders(folders)
        raise _translate(failure) from failure

    async def _still_exists(self, node_id: MirrorStableId) -> bool:
        path = await self._paths.path_of(node_id)
        return path is not None and await self._blocking(os.path.lexists, path)

    @asynccontextmanager
    async def download(
        self, record: NodeRecord, *, range_: slice | None
    ) -> AsyncGenerator[AsyncIterable[bytes], None]:
        path = await self._paths.path_of(record.id)
        state = (await self._store.get_entries([record.id])).get(record.id)
        if path is None or state is None:
            raise OperationUnavailableError(f"{record.id} has no local path")
        try:
            f = await self._blocking(open_verified, path, state.identity)
        except OSError as e:
            raise OperationUnavailableError(str(e)) from e
        try:
            yield self._chunks(f, range_)
        finally:
            await self._blocking(f.close)

    async def _chunks(self, f: BinaryIO, range_: slice | None) -> AsyncIterator[bytes]:
        size = await self._blocking(lambda: f.seek(0, 2))
        start, stop, _ = (range_ or slice(None)).indices(size)
        await self._blocking(f.seek, start)
        remaining = max(stop - start, 0)
        while remaining > 0:
            chunk = await self._blocking(f.read, min(_CHUNK_SIZE, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def _write_chunk(f: BinaryIO, digest: Any, chunk: bytes) -> None:
    f.write(chunk)
    digest.update(chunk)


def _upload_error(e: OSError, name: str) -> Exception:
    if e.errno in _NO_SPACE:
        return UploadPermanentError(str(e))
    if e.errno == errno.ENAMETOOLONG:
        return UploadNameTooLongError(name)
    if e.errno in _DENIED:
        return OperationNotSupportedError(f"Permission denied: {e.strerror}")
    return UploadTransientError(str(e))


class FilesystemUploader(_Writes):
    """Stages uploads next to their destination and renames them into place."""

    async def find_conflict(
        self, parent_id: MirrorStableId, name: str
    ) -> NodeRecord | None:
        self._check_writable()
        folder = await self._folder(parent_id)
        if not await self._blocking(os.path.lexists, os.path.join(folder, name)):
            return None
        return await self._mirrored_child(parent_id, name)

    async def upload(
        self,
        parent_id: MirrorStableId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        self._check_writable()
        _check_name(name)
        folder = await self._folder(parent_id)
        try:
            staging, f = await self._blocking(create_staging, folder)
        except OSError as e:
            raise _upload_error(e, name) from e
        digest = new_digest()
        try:
            # Errors from the request stream propagate unchanged; only failures
            # writing the staging file are translated.
            async for chunk in content:
                try:
                    await self._blocking(_write_chunk, f, digest, chunk)
                except OSError as e:
                    raise _upload_error(e, name) from e
            try:
                await self._blocking(finish_staging, f)
            except OSError as e:
                raise _upload_error(e, name) from e
        except BaseException:
            await self._blocking(discard_staging, staging, f)
            raise

        destination = os.path.join(folder, name)
        async with self._scanner.exclusive():
            async with self._node_sync.observe() as read:
                try:
                    await self._blocking(rename_no_replace, staging, destination)
                except FileExistsError:
                    pass
                except OSError as e:
                    await self._blocking(discard_staging, staging, f)
                    raise _upload_error(e, name) from e
                else:
                    entry = await self._entry(destination)
                    observation = FilesystemScanner.observation(
                        entry, new_node_id(), parent_id, digest.hexdigest()
                    )
                    if media_info is not None:
                        observation = replace(
                            observation,
                            record=replace(
                                observation.record,
                                is_image=media_info.is_image,
                                is_video=media_info.is_video,
                                width=media_info.width,
                                height=media_info.height,
                                ms_duration=media_info.ms_duration,
                            ),
                        )
                    return await self._record(observation, read)
        await self._blocking(discard_staging, staging, f)
        existing = await self._mirrored_child(parent_id, name)
        if existing is None:
            raise UploadPermanentError(
                f"{name!r} is taken by an entry that is not mirrored"
            )
        raise UploadConflictError(existing)
