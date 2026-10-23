"""High-level utility functions for cloud drive operations.

This module provides public utility functions that complement the Drive
interface with common operations like:
- Path manipulation and validation
- File transfers between local and cloud storage
- Node movement with path resolution
- Duplicate detection
- Change action handling with type-safe dispatching

These utilities are built on top of the Drive interface and provide
convenient, high-level abstractions for common patterns.

Example:
    Uploading and downloading files::

        >>> from pathlib import Path, PurePath
        >>> from wcpan.drive.client.lib import upload_file_from_local, download_file_to_local
        >>>
        >>> # Upload local file
        >>> node = await upload_file_from_local(
        ...     drive,
        ...     Path("/local/photo.jpg"),
        ...     parent,
        ...     mime_type="image/jpeg",
        ... )
        >>>
        >>> # Download to local directory
        >>> local_path = await download_file_to_local(
        ...     drive,
        ...     node,
        ...     Path("/downloads"),
        ... )
"""

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath
from typing import TypeGuard

from ._lib import (
    download_retry,
    resolve_path,
    upload_retry,
)
from .exceptions import NodeExistsError, NodeIsADirectoryError, NodeNotFoundError
from .types import (
    ChangeAction,
    Drive,
    MediaInfo,
    Node,
    RemoveAction,
    UpdateAction,
)


__all__ = (
    "dispatch_change",
    "download_file_to_local",
    "else_none",
    "find_duplicate_nodes",
    "is_remove",
    "is_update",
    "is_valid_name",
    "move_node",
    "normalize_path",
    "upload_file_from_local",
)


_DEFAULT_FILE_MIME_TYPE = "application/octet-stream"


def normalize_path(path: PurePath) -> PurePath:
    """Normalize an absolute path by resolving . and .. components.

    Processes path segments to resolve relative references (. and ..)
    while preserving the absolute path structure. This is more robust
    than PurePath's built-in normalization for cloud drive paths.

    Args:
        path: The absolute path to normalize.

    Returns:
        Normalized absolute PurePath with . and .. resolved.

    Raises:
        ValueError: If path is not absolute.

    Example:
        Normalizing paths::

            >>> from pathlib import PurePath
            >>> path = PurePath("/docs/../photos/./image.jpg")
            >>> normalized = normalize_path(path)
            >>> print(normalized)
            /photos/image.jpg

            >>> path = PurePath("/a/b/c/../../d")
            >>> print(normalize_path(path))
            /a/d
    """
    if not path.is_absolute():
        raise ValueError("only accepts absolute path")
    rv: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        elif part == ".." and rv[-1] != "/":
            rv.pop()
        else:
            rv.append(part)
    return PurePath(*rv)


def is_valid_name(name: str) -> bool:
    """Check if a name is valid for files and directories.

    Validates that a name doesn't contain path separators or other
    invalid characters. Valid names are simple filenames without
    any path components.

    Args:
        name: The name string to validate.

    Returns:
        True if the name is valid, False otherwise.

    Example:
        Validating names::

            >>> is_valid_name("document.txt")
            True
            >>> is_valid_name("my file.pdf")
            True
            >>> is_valid_name("path/to/file.txt")
            False
            >>> is_valid_name("file\\name.txt")
            False
            >>> is_valid_name("./relative")
            False
    """
    if name.find("\\") >= 0:
        return False
    path = Path(name)
    return path.name == name


async def else_none[T](aw: Awaitable[T]) -> T | None:
    """Execute an awaitable, returning None if NodeNotFoundError is raised.

    Convenience wrapper for operations that may fail due to missing nodes.
    Converts NodeNotFoundError exceptions into None returns for cleaner
    error handling.

    Args:
        aw: The awaitable to execute.

    Returns:
        The awaitable's result if successful, None if NodeNotFoundError.

    Example:
        Checking if a path exists::

            >>> node = await else_none(drive.get_node_by_path(path))
            >>> if node is None:
            ...     print("Path does not exist")
            ... else:
            ...     print(f"Found: {node.name}")

        Safe child lookup::

            >>> child = await else_none(drive.get_child_by_name("file.txt", parent))
            >>> if child:
            ...     await drive.delete(child)
    """
    try:
        return await aw
    except NodeNotFoundError:
        return None


async def move_node(
    drive: Drive,
    src_path: PurePath,
    dst_path: PurePath,
) -> Node:
    """Move or rename a node using path-based addressing.

    Provides flexible path-based node movement with support for:
    - Absolute destination paths
    - Relative destination paths
    - Renaming in place
    - Moving to parent directory (..)

    The function intelligently handles different path types and resolves
    them to appropriate Drive.move() calls.

    Args:
        drive: The Drive instance to operate on.
        src_path: Absolute path to the source node.
        dst_path: Destination path (absolute or relative to source parent).

    Returns:
        The moved/renamed Node with updated properties.

    Raises:
        NodeNotFoundError: If source path doesn't exist or destination
            parent doesn't exist.
        NodeExistsError: If destination is an existing file (won't overwrite).
        ValueError: If there's no valid path to the destination parent.

    Example:
        Renaming a file::

            >>> from pathlib import PurePath
            >>> moved = await move_node(
            ...     drive,
            ...     PurePath("/old_name.txt"),
            ...     PurePath("new_name.txt"),  # Relative path = rename
            ... )

        Moving to different directory::

            >>> moved = await move_node(
            ...     drive,
            ...     PurePath("/docs/file.txt"),
            ...     PurePath("/archive/file.txt"),  # Absolute path
            ... )

        Moving to parent directory::

            >>> moved = await move_node(
            ...     drive,
            ...     PurePath("/folder/subfolder/file.txt"),
            ...     PurePath(".."),  # Move up one level
            ... )

        Moving and renaming::

            >>> moved = await move_node(
            ...     drive,
            ...     PurePath("/docs/old.txt"),
            ...     PurePath("/archive/new.txt"),
            ... )

    Note:
        - If dst_path is ".", the node is unchanged
        - If dst_path is absolute and points to a directory, the node
          is moved into that directory keeping its name
        - If dst_path is absolute and doesn't exist, the node is moved
          to that exact path (parent directory must exist)
    """
    src_node = await drive.get_node_by_path(src_path)

    # case 1 - move to a relative path
    if not dst_path.is_absolute():
        # case 1.1 - a name, not path
        if dst_path.name == dst_path:
            # case 1.1.1 - move to the same directory, do nothing
            if dst_path.name == ".":
                return src_node
            # case 1.1.2 - rename only
            if dst_path.name != "..":
                return await drive.move(
                    src_node,
                    new_parent=None,
                    new_name=dst_path.name,
                )
            # case 1.1.3 - move to parent directory, the same as case 1.2

        # case 1.2 - a relative path, resolve to absolute path
        # NOTE PurePath does not implement normalizing algorithm
        dst_path = resolve_path(src_path.parent, dst_path)

    # case 2 - move to an absolute path
    dst_node = await else_none(drive.get_node_by_path(dst_path))
    # case 2.1 - the destination is empty
    if not dst_node:
        # move to the parent directory of the destination
        try:
            new_parent = await drive.get_node_by_path(dst_path.parent)
        except NodeNotFoundError as e:
            raise ValueError(f"no direct path to {dst_path}") from e
        return await drive.move(src_node, new_parent=new_parent, new_name=dst_path.name)
    # case 2.2 - the destination is a file
    if not dst_node.is_directory:
        # do not overwrite existing file
        raise NodeExistsError(dst_node)
    # case 2.3 - the distination is a directory
    return await drive.move(src_node, new_parent=dst_node, new_name=None)


async def find_duplicate_nodes(
    drive: Drive,
    root_node: Node | None = None,
) -> list[list[Node]]:
    """Find nodes with duplicate names in the same directory.

    Walks the directory tree and identifies any directories or files
    that share the same name within a single parent directory. This
    indicates filesystem inconsistencies that may need resolution.

    Args:
        drive: The Drive instance to search.
        root_node: Starting node for search. If None, uses drive root.

    Returns:
        List of duplicate groups, where each group is a list of Nodes
        with the same name in the same parent. Empty list if no duplicates.

    Example:
        Finding duplicates::

            >>> duplicates = await find_duplicate_nodes(drive)
            >>> for group in duplicates:
            ...     print(f"Duplicate name '{group[0].name}':")
            ...     for node in group:
            ...         path = await drive.resolve_path(node)
            ...         print(f"  - {path} (ID: {node.id})")

        Searching within a specific directory::

            >>> docs = await drive.get_node_by_path(PurePath("/Documents"))
            >>> duplicates = await find_duplicate_nodes(drive, docs)
            >>> if not duplicates:
            ...     print("No duplicates found in Documents")

    Note:
        Cloud storage services should prevent duplicates, but they can
        occur due to sync conflicts, API race conditions, or service bugs.
    """
    if not root_node:
        root_node = await drive.get_root()

    rv: list[list[Node]] = []
    async for _root, directorys, files in drive.walk(root_node):
        nodes = directorys + files
        seen: dict[str, list[Node]] = {}
        for node in nodes:
            if node.name not in seen:
                seen[node.name] = [node]
            else:
                seen[node.name].append(node)
        for nodes in seen.values():
            if len(nodes) > 1:
                rv.append(nodes)

    return rv


async def upload_file_from_local(
    drive: Drive,
    path: Path,
    parent: Node,
    *,
    name: str | None = None,
    mime_type: str | None = None,
    media_info: MediaInfo | None = None,
    timeout: float | None = None,
) -> Node:
    """Upload a local file to the cloud drive.

    Reads a local file and uploads it to the specified parent directory
    in the cloud. Supports resumable uploads with automatic retry on
    timeout, making it suitable for large files or unstable connections.

    Args:
        drive: The Drive instance to upload to.
        path: Absolute path to the local file to upload.
        parent: Parent directory Node where file will be uploaded.
        name: Name for the uploaded file. If None, uses local filename.
        mime_type: MIME type for the file. If None, uses
            'application/octet-stream'.
        media_info: Optional MediaInfo for images/videos.
        timeout: Timeout in seconds for each I/O operation. None means
            no timeout. If an operation times out, upload automatically
            resumes from the last successful position.

    Returns:
        The newly created Node representing the uploaded file.

    Raises:
        ValueError: If path doesn't exist or is not a file.
        NodeExistsError: If a file with the same name already exists
            in the parent directory.
        AuthenticationError: If the drive is not authenticated.

    Example:
        Basic upload::

            >>> from pathlib import Path, PurePath
            >>> root = await drive.get_root()
            >>> node = await upload_file_from_local(
            ...     drive,
            ...     Path("/home/user/document.pdf"),
            ...     root,
            ... )
            >>> print(f"Uploaded: {node.name} ({node.size} bytes)")

        Upload with custom name::

            >>> node = await upload_file_from_local(
            ...     drive,
            ...     Path("/tmp/temp_file.dat"),
            ...     parent,
            ...     name="final_name.dat",
            ... )

        Upload image with metadata::

            >>> from wcpan.drive.client.types import MediaInfo
            >>> media = MediaInfo.image(width=1920, height=1080)
            >>> node = await upload_file_from_local(
            ...     drive,
            ...     Path("/photos/vacation.jpg"),
            ...     parent,
            ...     mime_type="image/jpeg",
            ...     media_info=media,
            ... )

        Upload with timeout for resilience::

            >>> node = await upload_file_from_local(
            ...     drive,
            ...     Path("/large_file.zip"),
            ...     parent,
            ...     timeout=30.0,  # 30 second timeout per chunk
            ... )

    Note:
        The function automatically handles upload resumption on timeout.
        File reading also has timeout protection for unstable filesystems.
    """
    # sanity check
    path = path.resolve()
    if not path.is_file():
        raise ValueError("invalid file")

    file_name = path.name if name is None else name
    total_file_size = path.stat().st_size
    if not mime_type:
        mime_type = _DEFAULT_FILE_MIME_TYPE

    async with drive.upload_file(
        name=file_name,
        parent=parent,
        size=total_file_size,
        mime_type=mime_type,
        media_info=media_info,
    ) as fout:
        with open(path, "rb") as fin:
            await upload_retry(fin, fout, timeout)
        node = await fout.node()
    return node


async def download_file_to_local(
    drive: Drive,
    node: Node,
    path: Path,
    *,
    timeout: float | None = None,
) -> Path:
    """Download a cloud file to a local directory.

    Downloads a file from the cloud to the specified local directory.
    Supports resumable downloads with automatic retry on timeout, making
    it suitable for large files or unstable connections.

    If the download is interrupted, the next call will resume from where
    it left off using a temporary file with .__tmp__ suffix.

    Args:
        drive: The Drive instance to download from.
        node: The file Node to download.
        path: Local directory path where file will be saved.
        timeout: Timeout in seconds for each I/O operation. None means
            no timeout. If an operation times out, download automatically
            resumes from the last successful position.

    Returns:
        Path to the downloaded file (path / node.name).

    Raises:
        ValueError: If node is a directory or path is not a directory.
        NodeIsADirectoryError: If a directory exists at the destination
            path or temporary path location.
        AuthenticationError: If the drive is not authenticated.

    Example:
        Basic download::

            >>> from pathlib import Path, PurePath
            >>> node = await drive.get_node_by_path(PurePath("/document.pdf"))
            >>> local_path = await download_file_to_local(
            ...     drive,
            ...     node,
            ...     Path("/downloads"),
            ... )
            >>> print(f"Downloaded to: {local_path}")

        Download with timeout for resilience::

            >>> local_path = await download_file_to_local(
            ...     drive,
            ...     node,
            ...     Path("/downloads"),
            ...     timeout=30.0,  # 30 second timeout per chunk
            ... )

        Handling existing files::

            >>> dest_dir = Path("/downloads")
            >>> local_path = await download_file_to_local(drive, node, dest_dir)
            >>> # If file already exists, returns existing path without re-downloading

        Resuming interrupted download::

            >>> # First attempt (interrupted)
            >>> try:
            ...     local_path = await download_file_to_local(drive, node, dest_dir)
            ... except asyncio.TimeoutError:
            ...     print("Download interrupted")
            >>>
            >>> # Second attempt (resumes automatically)
            >>> local_path = await download_file_to_local(drive, node, dest_dir)
            >>> # Continues from where it left off using .tmp file

    Note:
        - If the file already exists and is complete, it's returned immediately
          without re-downloading
        - Interrupted downloads leave a .__tmp__ file that's used to resume
        - Empty files (size 0) are created without downloading
        - The function handles both filesystem and network timeouts
    """
    if node.is_directory:
        raise ValueError(f"cannot download a directory")

    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")

    # check if exists
    complete_path = path.joinpath(node.name)
    if complete_path.is_file():
        return complete_path

    # exists but not a file
    if complete_path.exists():
        raise NodeIsADirectoryError(complete_path)

    # if the file is empty, no need to download
    if node.size <= 0:
        open(complete_path, "w").close()
        return complete_path

    # resume download
    tmp_path = complete_path.parent.joinpath(f"{complete_path.name}.__tmp__")
    if tmp_path.is_file():
        offset = tmp_path.stat().st_size
        if offset > node.size:
            raise RuntimeError(
                f"local file size of `{complete_path}` is greater then remote"
                f" ({offset} > {node.size})"
            )
    elif tmp_path.exists():
        raise NodeIsADirectoryError(complete_path)
    else:
        offset = 0

    if offset < node.size:
        async with drive.download_file(node) as fin:
            await fin.seek(offset)
            with open(tmp_path, "ab") as fout:
                await download_retry(fin, fout, timeout)

    # rename it back if completed
    tmp_path.rename(complete_path)

    return complete_path


def is_remove(change: ChangeAction, /) -> TypeGuard[RemoveAction]:
    """Check if a change action represents a node removal.

    Type guard function that narrows ChangeAction to RemoveAction,
    enabling type-safe access to removal information.

    Args:
        change: The ChangeAction to check.

    Returns:
        True if the change is a RemoveAction, False otherwise.

    Example:
        Using with type narrowing::

            >>> async for change in drive.sync():
            ...     if is_remove(change):
            ...         removed, node_id = change  # Type is RemoveAction
            ...         print(f"Removed: {node_id}")

        Filtering removed nodes::

            >>> changes = []
            >>> async for change in drive.sync():
            ...     changes.append(change)
            >>> removed_ids = [node_id for c in changes if is_remove(c) for _, node_id in [c]]

    See Also:
        - is_update(): Check if change is an UpdateAction
        - dispatch_change(): Pattern matching alternative
    """
    return change[0]


def is_update(change: ChangeAction, /) -> TypeGuard[UpdateAction]:
    """Check if a change action represents a node update or addition.

    Type guard function that narrows ChangeAction to UpdateAction,
    enabling type-safe access to the updated Node.

    Args:
        change: The ChangeAction to check.

    Returns:
        True if the change is an UpdateAction, False otherwise.

    Example:
        Using with type narrowing::

            >>> async for change in drive.sync():
            ...     if is_update(change):
            ...         updated, node = change  # Type is UpdateAction
            ...         print(f"Updated: {node.name}")

        Processing only updates::

            >>> changes = []
            >>> async for change in drive.sync():
            ...     changes.append(change)
            >>> updated_nodes = [node for c in changes if is_update(c) for _, node in [c]]

    See Also:
        - is_remove(): Check if change is a RemoveAction
        - dispatch_change(): Pattern matching alternative
    """
    return not change[0]


def dispatch_change[R](
    change: ChangeAction,
    /,
    *,
    on_remove: Callable[[str], R],
    on_update: Callable[[Node], R],
) -> R:
    """Dispatch a change action to the appropriate handler function.

    Provides pattern matching style dispatching for ChangeAction handling.
    Calls on_remove for removals or on_update for updates/additions.

    Args:
        change: The ChangeAction to dispatch.
        on_remove: Callback for RemoveAction. Receives the removed node's ID.
        on_update: Callback for UpdateAction. Receives the updated Node.

    Returns:
        The return value from whichever callback was invoked.

    Example:
        Basic dispatching::

            >>> def handle_remove(node_id: str) -> None:
            ...     print(f"Removed: {node_id}")
            ...
            >>> def handle_update(node: Node) -> None:
            ...     print(f"Updated: {node.name}")
            ...
            >>> async for change in drive.sync():
            ...     dispatch_change(
            ...         change,
            ...         on_remove=handle_remove,
            ...         on_update=handle_update,
            ...     )

        Collecting statistics::

            >>> stats = {"removed": 0, "updated": 0}
            >>> async for change in drive.sync():
            ...     dispatch_change(
            ...         change,
            ...         on_remove=lambda _: stats.update(removed=stats["removed"] + 1),
            ...         on_update=lambda _: stats.update(updated=stats["updated"] + 1),
            ...     )
            >>> print(f"Changes: {stats['removed']} removed, {stats['updated']} updated")

        Building index::

            >>> index: dict[str, Node] = {}
            >>> async for change in drive.sync():
            ...     dispatch_change(
            ...         change,
            ...         on_remove=lambda id_: index.pop(id_, None),
            ...         on_update=lambda node: index.update({node.id: node}),
            ...     )

    See Also:
        - is_remove() and is_update(): Type guard alternatives
    """
    match change:
        case (True, id_):
            return on_remove(id_)
        case (False, node):
            return on_update(node)
