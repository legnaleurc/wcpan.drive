"""Exception classes for cloud drive operations.

This module defines the exception hierarchy for wcpan.drive.client. All
exceptions inherit from DriveError, which inherits from the standard Exception.

Exception Hierarchy:
    DriveError (base exception)
    ├── InvalidServiceError
    ├── CacheError
    ├── NodeExistsError
    ├── NodeNotFoundError
    ├── NodeIsADirectoryError
    ├── AuthenticationError
    └── UnsupportedHashAlgorithmError

Common usage patterns:
    - Catch DriveError to handle all drive-related errors
    - Catch specific exceptions for targeted error handling
    - Use NodeExistsError.node to access the conflicting node
    - Use NodeNotFoundError.id to identify the missing node
"""

from pathlib import Path

from .types import Node


__all__ = (
    "DriveError",
    "InvalidServiceError",
    "CacheError",
    "NodeExistsError",
    "NodeNotFoundError",
    "NodeIsADirectoryError",
    "AuthenticationError",
    "UnsupportedHashAlgorithmError",
)


class DriveError(Exception):
    """Base exception for all cloud drive operations.

    All exceptions raised by wcpan.drive.client inherit from this class.
    Catch this exception to handle any drive-related error.

    Example:
        Handling all drive errors::

            >>> try:
            ...     node = await drive.get_node_by_path(path)
            ... except DriveError as e:
            ...     print(f"Drive operation failed: {e}")
    """

    pass


class InvalidServiceError(DriveError):
    """Raised when service API version does not match requirements.

    This exception occurs during drive creation when a Backend or
    backend middleware reports an API version that differs from the
    expected version. This typically indicates incompatible service
    implementations or middleware.

    Example:
        Handling version mismatches::

            >>> try:
            ...     async with create_drive(backend=my_service, cache_path=path) as drive:
            ...         pass
            ... except InvalidServiceError as e:
            ...     print(f"Service version mismatch: {e}")
    """

    pass


class CacheError(DriveError):
    """Raised when the local metadata cache cannot be used.

    The usual cause is a cache file written with an older schema; delete
    the file and sync again to rebuild it.
    """

    pass


class NodeExistsError(DriveError):
    """Raised when attempting to create a node that already exists.

    This exception is raised when operations would create duplicate nodes,
    such as uploading a file or creating a directory with a name that
    already exists in the target parent directory.

    Attributes:
        node: The existing Node that conflicts with the operation.

    Example:
        Handling duplicate nodes::

            >>> try:
            ...     await drive.create_directory("documents", parent)
            ... except NodeExistsError as e:
            ...     print(f"Already exists: {e.node.name}")
            ...     existing_node = e.node

        Avoiding the exception with exist_ok::

            >>> node = await drive.create_directory(
            ...     "documents",
            ...     parent,
            ...     exist_ok=True,
            ... )
    """

    def __init__(self, node: Node) -> None:
        super().__init__(f"node already exists: {node.name}")
        self.node = node


class NodeNotFoundError(DriveError):
    """Raised when a requested node cannot be found.

    This exception occurs when attempting to access a node that does not
    exist, either by path or by ID. Common causes include:
    - Requesting a non-existent path
    - Using an invalid or stale node ID
    - The node was deleted after obtaining its ID

    Attributes:
        id: The node ID or path string that could not be found.

    Example:
        Handling missing nodes::

            >>> try:
            ...     node = await drive.get_node_by_path(PurePath("/missing/file.txt"))
            ... except NodeNotFoundError as e:
            ...     print(f"Not found: {e.id}")

        Using else_none helper to avoid exception::

            >>> from wcpan.drive.client.lib import else_none
            >>> node = await else_none(drive.get_node_by_path(path))
            >>> if node is None:
            ...     print("File not found")
    """

    def __init__(self, id: str) -> None:
        super().__init__(f"node not found: {id}")
        self.id = id


class NodeIsADirectoryError(DriveError):
    """Raised when a file operation is attempted on a directory.

    This exception occurs when code expects a file but encounters a
    directory instead. Common scenarios include:
    - Attempting to download a directory node
    - Finding a directory at a path where a file was expected
    - Local filesystem conflicts during download operations

    Attributes:
        path: The Path object representing the directory.

    Example:
        Handling directory conflicts::

            >>> try:
            ...     await download_file_to_local(drive, node, local_dir)
            ... except NodeIsADirectoryError as e:
            ...     print(f"Cannot download directory: {e.path}")

        Checking before download::

            >>> if not node.is_directory:
            ...     await download_file_to_local(drive, node, local_dir)
            ... else:
            ...     print("Node is a directory, skipping")
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is a directory")
        self.path = path


class AuthenticationError(DriveError):
    """Raised when authentication fails or is required but missing.

    This exception occurs when attempting drive operations that require
    authentication but the drive is not authenticated. Most write
    operations (upload, delete, move) and some read operations require
    authentication.

    Note:
        Call drive.authenticate() before performing authenticated operations.
        Use drive.is_authenticated() to check authentication status.

    Example:
        Handling authentication errors::

            >>> try:
            ...     await drive.upload_file(name, parent, size=size)
            ... except AuthenticationError:
            ...     await drive.authenticate()
            ...     await drive.upload_file(name, parent, size=size)

        Checking authentication proactively::

            >>> if not await drive.is_authenticated():
            ...     await drive.authenticate()
            >>> await drive.upload_file(name, parent, size=size)
    """

    pass


class UnsupportedHashAlgorithmError(DriveError):
    """Raised when no hasher is available for a node's hash algorithm.

    This exception occurs when requesting a hasher factory for a node whose
    ``hash_algorithm`` the service cannot compute, including directories and
    files without a checksum. Services never fall back to another algorithm.

    Attributes:
        node: The node whose algorithm is unsupported.
        algorithm: The unsupported algorithm identifier, possibly empty.

    Example:
        Handling unsupported algorithms::

            >>> try:
            ...     factory = await drive.get_hasher_factory(node)
            ... except UnsupportedHashAlgorithmError as e:
            ...     print(f"Cannot verify {e.node.name}: {e.algorithm!r}")
    """

    def __init__(self, node: Node) -> None:
        super().__init__(
            f"unsupported hash algorithm {node.hash_algorithm!r} for node {node.id}"
        )
        self.node = node
        self.algorithm = node.hash_algorithm
