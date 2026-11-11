"""Core type definitions and abstract interfaces for cloud drive operations.

This module defines the foundational types, data structures, and abstract
interfaces that form the wcpan.drive.client API. All implementations must
adhere to these contracts.

Key Concepts:
    - Node: Immutable representation of files and directories
    - Drive: High-level interface for drive operations
    - Backend: Cloud storage API operations
    - Drive also keeps a local SQLite cache of node metadata per source
    - MediaInfo: Metadata for image and video files

The module follows a layered architecture:
    1. Drive (public API) - User-facing operations
    2. Backend - Pluggable implementations
    3. Node + MediaInfo - Data representations

Thread Safety:
    All interfaces are designed for async/await usage. Implementations
    should handle concurrent access appropriately.

Example:
    Basic usage pattern::

        >>> async with create_drive(backend=my_backend, cache_path="drive.sqlite") as drive:
        ...     root = await drive.get_root()
        ...     files = await drive.get_children(root)
"""

from abc import ABCMeta, abstractmethod
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
)
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Literal, Self


__all__ = (
    "Backend",
    "BackendFactory",
    "BackendMiddleware",
    "ChangeAction",
    "Hasher",
    "MediaInfo",
    "Node",
    "NodeQuery",
    "NodeQueryBuilder",
    "PrivateDict",
    "ReadableFile",
    "RemoveAction",
    "SourceConfig",
    "UpdateAction",
    "WritableFile",
)


type PrivateDict = dict[str, str]
"""Service-specific metadata dictionary for custom attributes.

Implementations can store arbitrary string key-value pairs to support
service-specific features not covered by the standard Node attributes.
For example, sharing permissions, labels, or custom properties.
"""


@dataclass(frozen=True, kw_only=True)
class Node:
    """Immutable representation of a file or directory in the cloud drive.

    Node is the core data structure representing both files and directories.
    All attributes are immutable - to modify a node, use Drive methods which
    return new Node instances with updated values.

    Attributes:
        id: Unique identifier for this node assigned by the service.
        parent_id: ID of the parent directory, or None for root node.
        name: Name of the file or directory (without path).
        is_directory: True if this node represents a directory.
        is_trashed: True if this node is in the trash, either directly or
            because an ancestor directory is.
        created_time: Creation timestamp.
        modified_time: Content modification timestamp.
        changed_time: Last backend-observable node record change timestamp.
        mime_type: MIME type string (e.g., "image/jpeg", "video/mp4").
        hash: Content digest for files, empty string for directories.
        hash_algorithm: Identifier of the algorithm that produced ``hash``
            (e.g., "md5"). Empty string for directories. A file has either
            both ``hash`` and ``hash_algorithm`` or neither.
        size: File size in bytes, 0 for directories.
        is_image: True if this is an image file with media metadata.
        is_video: True if this is a video file with media metadata.
        width: Image/video width in pixels, 0 if not applicable.
        height: Image/video height in pixels, 0 if not applicable.
        ms_duration: Video duration in milliseconds, 0 if not applicable.
        private: Service-specific metadata, or None if not used.

    Note:
        Node is frozen (immutable). To change properties, use Drive.move()
        or other Drive methods which return new Node instances.

    Example:
        Accessing node properties::

            >>> node = await drive.get_node_by_path(PurePath("/photos/image.jpg"))
            >>> print(f"{node.name}: {node.size} bytes")
            >>> if node.is_image:
            ...     print(f"Dimensions: {node.width}x{node.height}")

        Checking if node is root::

            >>> root = await drive.get_root()
            >>> is_root = node.parent_id is None
    """

    id: str
    parent_id: str | None
    name: str
    is_directory: bool
    is_trashed: bool
    created_time: datetime
    modified_time: datetime
    changed_time: datetime
    mime_type: str
    hash: str
    hash_algorithm: str
    size: int
    is_image: bool
    is_video: bool
    width: int
    height: int
    ms_duration: int
    private: PrivateDict | None

    def __post_init__(self) -> None:
        if self.is_directory:
            if self.hash or self.hash_algorithm:
                raise ValueError(f"directory {self.id} must not have a hash")
        elif bool(self.hash) != bool(self.hash_algorithm):
            raise ValueError(
                f"file {self.id} must have both hash and hash_algorithm, or neither"
            )


@dataclass(frozen=True, kw_only=True)
class NodeQuery:
    """Filters for node queries and summaries on Drive.

    All conditions are combined with AND; None means "no condition". Build
    one with Drive.find_nodes() instead of constructing it directly.

    Attributes:
        ancestor_id: Only match descendants of this node (excluding itself).
        is_directory: Only match directories (True) or files (False).
        is_trashed: Match trashed (True), non-trashed (False, the default),
            or both (None). Node.is_trashed already covers nodes inside a
            trashed directory, so no ancestor check is needed.
        name_pattern: Case-insensitive regex searched in the node name.
        mime_type_prefix: Only match files whose MIME type starts with this.
        size_min: Only match files at least this many bytes.
        size_max: Only match files at most this many bytes.
        created_after: Only match nodes created at or after this time.
        created_before: Only match nodes created before this time.
        modified_after: Only match nodes modified at or after this time.
        modified_before: Only match nodes modified before this time.
        limit: Return at most this many nodes, in no particular order.

    Note:
        Size and MIME type conditions only match files.

    Raises:
        ValueError: If the conditions are invalid or contradictory.
    """

    ancestor_id: str | None = None
    is_directory: bool | None = None
    is_trashed: bool | None = False
    name_pattern: str | None = None
    mime_type_prefix: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        import re

        if self.name_pattern is not None:
            try:
                re.compile(self.name_pattern, re.I)
            except re.error as e:
                raise ValueError(f"invalid name pattern: {e}") from e
        if self.mime_type_prefix == "":
            raise ValueError("MIME type prefix must not be empty")
        for size in (self.size_min, self.size_max):
            if size is not None and size < 0:
                raise ValueError("size must not be negative")
        _check_range("size", self.size_min, self.size_max)
        for time in (
            self.created_after,
            self.created_before,
            self.modified_after,
            self.modified_before,
        ):
            if time is not None and time.utcoffset() is None:
                raise ValueError("time must be timezone-aware")
        _check_range("created time", self.created_after, self.created_before)
        _check_range("modified time", self.modified_after, self.modified_before)
        if self.is_directory and self.has_file_conditions:
            raise ValueError("size and MIME type conditions only match files")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be positive")

    @property
    def has_file_conditions(self) -> bool:
        """True if any condition only matches files."""
        return (
            self.mime_type_prefix is not None
            or self.size_min is not None
            or self.size_max is not None
        )


def _check_range[T: (int, datetime)](name: str, low: T | None, high: T | None):
    if low is not None and high is not None and low > high:
        raise ValueError(f"{name} range is empty")


@dataclass(frozen=True, kw_only=True)
class NodeSummary:
    """Aggregate of matching nodes: directories count but contribute no size."""

    count: int
    total_size: int


class NodeQueryBuilder:
    """Fluent builder for NodeQuery, bound to retrieval and summary functions.

    Each method updates the builder and returns it; calling a method again
    replaces that condition. Get one from Drive.find_nodes().

    Example:
        Finding empty files under a directory::

            >>> nodes = await drive.find_nodes().descendant_of(node).size(max=0).all()

        Finding large videos modified this year::

            >>> nodes = await (
            ...     drive.find_nodes()
            ...     .mime_type("video/")
            ...     .size(min=100 * 1024**2)
            ...     .modified(after=datetime(2026, 1, 1, tzinfo=UTC))
            ...     .all()
            ... )
    """

    def __init__(
        self,
        run: Callable[[NodeQuery], Awaitable[list[Node]]],
        *,
        summarize: Callable[[NodeQuery], Awaitable[NodeSummary]],
    ) -> None:
        self._run = run
        self._summarize = summarize
        self._fields: dict[str, object] = {}

    def descendant_of(self, node: Node, /) -> Self:
        """Only match descendants of *node*, excluding *node* itself."""
        self._fields["ancestor_id"] = node.id
        return self

    def files_only(self) -> Self:
        self._fields["is_directory"] = False
        return self

    def directories_only(self) -> Self:
        self._fields["is_directory"] = True
        return self

    def include_trashed(self) -> Self:
        """Match both trashed and non-trashed nodes."""
        self._fields["is_trashed"] = None
        return self

    def trashed_only(self) -> Self:
        self._fields["is_trashed"] = True
        return self

    def name_matches(self, pattern: str, /) -> Self:
        """Only match names where the case-insensitive regex *pattern* is found."""
        self._fields["name_pattern"] = pattern
        return self

    def mime_type(self, prefix: str, /) -> Self:
        """Only match files whose MIME type starts with *prefix*."""
        self._fields["mime_type_prefix"] = prefix
        return self

    def size(self, *, min: int | None = None, max: int | None = None) -> Self:
        """Only match files with *min* <= size <= *max*."""
        self._fields["size_min"] = min
        self._fields["size_max"] = max
        return self

    def created(
        self, *, after: datetime | None = None, before: datetime | None = None
    ) -> Self:
        """Only match nodes with *after* <= created time < *before*."""
        self._fields["created_after"] = after
        self._fields["created_before"] = before
        return self

    def modified(
        self, *, after: datetime | None = None, before: datetime | None = None
    ) -> Self:
        """Only match nodes with *after* <= modified time < *before*."""
        self._fields["modified_after"] = after
        self._fields["modified_before"] = before
        return self

    def limit(self, count: int, /) -> Self:
        self._fields["limit"] = count
        return self

    def to_query(self) -> NodeQuery:
        """Build the query without running it.

        Raises:
            ValueError: If the conditions are invalid or contradictory.
        """
        return NodeQuery(**self._fields)  # pyright: ignore[reportArgumentType]

    async def all(self) -> list[Node]:
        """Run the query and return all matching nodes, in no particular order.

        Raises:
            ValueError: If the conditions are invalid or contradictory.
        """
        return await self._run(self.to_query())

    async def summarize(self) -> NodeSummary:
        """Count matching nodes and sum file sizes without loading nodes.

        Raises:
            ValueError: If conditions are invalid or a limit is set.
        """
        query = self.to_query()
        if query.limit is not None:
            raise ValueError("summaries do not support limit")
        return await self._summarize(query)


type RemoveAction = tuple[Literal[True], str]
"""Change action representing a removed node.

A tuple of (True, node_id) indicating that the node with the given ID
was removed from the drive.

Example:
    Pattern matching on RemoveAction::

        >>> if change[0]:  # RemoveAction
        ...     removed, node_id = change
        ...     print(f"Node removed: {node_id}")
"""


type UpdateAction = tuple[Literal[False], Node]
"""Change action representing an updated or added node.

A tuple of (False, node) indicating that the node was either newly created
or had its metadata updated.

Example:
    Pattern matching on UpdateAction::

        >>> if not change[0]:  # UpdateAction
        ...     updated, node = change
        ...     print(f"Node updated: {node.name}")
"""


type ChangeAction = RemoveAction | UpdateAction
"""Union of change actions returned by drive synchronization.

Represents either a node removal (RemoveAction) or node update (UpdateAction).
Use type guard functions is_remove() and is_update() for type-safe handling.

Example:
    Using type guards::

        >>> from wcpan.drive.client.lib import is_remove, is_update
        >>> async for change in drive.sync():
        ...     if is_remove(change):
        ...         removed, node_id = change
        ...         print(f"Removed: {node_id}")
        ...     elif is_update(change):
        ...         updated, node = change
        ...         print(f"Updated: {node.name}")

    Using dispatch helper::

        >>> from wcpan.drive.client.lib import dispatch_change
        >>> result = dispatch_change(
        ...     change,
        ...     on_remove=lambda id: print(f"Removed {id}"),
        ...     on_update=lambda node: print(f"Updated {node.name}"),
        ... )
"""


class MediaInfo:
    """Media metadata for image and video files.

    Encapsulates dimension and duration information for media files.
    Use the factory methods image() or video() to create instances with
    appropriate metadata.

    Factory Methods:
        image(width, height): Create metadata for an image file.
        video(width, height, ms_duration): Create metadata for a video file.

    Properties:
        is_image: True if this represents image metadata.
        is_video: True if this represents video metadata.
        width: Width in pixels.
        height: Height in pixels.
        ms_duration: Duration in milliseconds (video only).

    Example:
        Creating image metadata::

            >>> media = MediaInfo.image(width=1920, height=1080)
            >>> await upload_file_from_local(
            ...     drive,
            ...     path,
            ...     parent,
            ...     mime_type="image/jpeg",
            ...     media_info=media,
            ... )

        Creating video metadata::

            >>> media = MediaInfo.video(width=1920, height=1080, ms_duration=120000)
            >>> await upload_file_from_local(
            ...     drive,
            ...     path,
            ...     parent,
            ...     mime_type="video/mp4",
            ...     media_info=media,
            ... )
    """

    @classmethod
    def image(cls, width: int, height: int) -> Self:
        """Create MediaInfo for an image file.

        Args:
            width: Image width in pixels.
            height: Image height in pixels.

        Returns:
            MediaInfo instance with is_image=True.
        """
        return cls(is_image=True, width=width, height=height)

    @classmethod
    def video(cls, width: int, height: int, ms_duration: int) -> Self:
        """Create MediaInfo for a video file.

        Args:
            width: Video width in pixels.
            height: Video height in pixels.
            ms_duration: Video duration in milliseconds.

        Returns:
            MediaInfo instance with is_video=True.
        """
        return cls(
            is_video=True,
            width=width,
            height=height,
            ms_duration=ms_duration,
        )

    def __init__(
        self,
        *,
        is_image: bool = False,
        is_video: bool = False,
        width: int = 0,
        height: int = 0,
        ms_duration: int = 0,
    ) -> None:
        self._is_image = is_image
        self._is_video = is_video
        self._width = width
        self._height = height
        self._ms_duration = ms_duration

    def __str__(self) -> str:
        if self.is_image:
            return f"MediaInfo(is_image=True, width={self.width}, height={self.height})"
        if self.is_video:
            return f"MediaInfo(is_video=True, width={self.width}, height={self.height}, ms_duration={self.ms_duration})"
        return "MediaInfo()"

    @property
    def is_image(self) -> bool:
        """True if this represents image metadata."""
        return self._is_image

    @property
    def is_video(self) -> bool:
        """True if this represents video metadata."""
        return self._is_video

    @property
    def width(self) -> int:
        """Width in pixels, or 0 if not applicable."""
        return self._width

    @property
    def height(self) -> int:
        """Height in pixels, or 0 if not applicable."""
        return self._height

    @property
    def ms_duration(self) -> int:
        """Duration in milliseconds for video, or 0 if not applicable."""
        return self._ms_duration


class ReadableFile(AsyncIterable[bytes], metaclass=ABCMeta):
    """Async readable file interface for downloading cloud files.

    Provides sequential and seekable read access to cloud file content.
    Returned by Drive.download_file() and Backend.download_file()
    as an async context manager.

    This interface extends AsyncIterable[bytes], allowing iteration over
    file chunks.

    Thread Safety:
        Not thread-safe. Each ReadableFile instance should be used from
        a single async task.

    Example:
        Basic download pattern::

            >>> async with drive.download_file(node) as file:
            ...     data = await file.read(8192)
            ...     while data:
            ...         process(data)
            ...         data = await file.read(8192)

        Using as async iterator::

            >>> async with drive.download_file(node) as file:
            ...     async for chunk in file:
            ...         process(chunk)

        Seeking and resuming::

            >>> async with drive.download_file(node) as file:
            ...     await file.seek(1024)  # Skip first 1KB
            ...     data = await file.read(4096)
    """

    @abstractmethod
    async def read(self, length: int) -> bytes:
        """Read at most length bytes from the file.

        Reads up to the specified number of bytes from the current position.
        Returns fewer bytes if EOF is reached.

        Args:
            length: Maximum number of bytes to read.

        Returns:
            Bytes read from the file. Empty bytes object indicates EOF.

        Example:
            Reading in chunks::

                >>> chunk = await file.read(8192)
                >>> if chunk:
                ...     print(f"Read {len(chunk)} bytes")
        """

    @abstractmethod
    async def seek(self, offset: int) -> int:
        """Seek to the specified position in the file.

        Moves the file pointer to the given absolute offset from the
        beginning of the file. Subsequent reads will start from this position.

        Args:
            offset: Absolute byte offset from the beginning of the file.

        Returns:
            The new absolute position.

        Example:
            Resume download from specific position::

                >>> await file.seek(1048576)  # Seek to 1MB
                >>> data = await file.read(8192)
        """

    @abstractmethod
    async def node(self) -> Node:
        """Get the Node being read.

        Returns the Node instance representing the file being downloaded.
        Useful for accessing metadata during download.

        Returns:
            The Node instance for this file.

        Example:
            Accessing node during download::

                >>> async with drive.download_file(node) as file:
                ...     current_node = await file.node()
                ...     print(f"Downloading: {current_node.name}")
        """


class Hasher(metaclass=ABCMeta):
    """Hash calculator for file integrity verification.

    Provides streaming hash calculation compatible with the cloud service's
    hash algorithm. Implementations must match the hashing method used by
    the specific cloud storage backend.

    Supports incremental updates for efficient hashing of large files
    and can be copied mid-stream for checkpointing.

    Example:
        Computing file hash::

            >>> hasher_factory = await drive.get_hasher_factory(node)
            >>> hasher = await hasher_factory()
            >>> with open("file.txt", "rb") as f:
            ...     while chunk := f.read(8192):
            ...         await hasher.update(chunk)
            >>> file_hash = await hasher.hexdigest()

        Using copy for checkpointing::

            >>> hasher1 = await hasher_factory()
            >>> await hasher1.update(b"data")
            >>> hasher2 = await hasher1.copy()
            >>> await hasher1.update(b"more")
            >>> await hasher2.update(b"different")
            >>> hash1 = await hasher1.hexdigest()
            >>> hash2 = await hasher2.hexdigest()
    """

    @abstractmethod
    async def update(self, data: bytes) -> None:
        """Feed data into the hash calculation.

        Updates the hash with the provided data. Can be called multiple
        times to process data incrementally.

        Args:
            data: Bytes to include in the hash calculation.

        Example:
            Incremental hashing::

                >>> await hasher.update(b"Hello ")
                >>> await hasher.update(b"World")
                >>> digest = await hasher.hexdigest()
        """

    @abstractmethod
    async def digest(self) -> bytes:
        """Get the raw binary hash digest.

        Returns the hash as raw bytes. The length depends on the hash
        algorithm (e.g., 16 bytes for MD5, 32 bytes for SHA-256).

        Returns:
            Raw binary hash digest.

        Example:
            Getting binary digest::

                >>> raw_hash = await hasher.digest()
                >>> print(f"Hash length: {len(raw_hash)} bytes")
        """

    @abstractmethod
    async def hexdigest(self) -> str:
        """Get the hexadecimal hash digest.

        Returns the hash as a lowercase hexadecimal string, which is
        typically used for comparison with Node.hash values.

        Returns:
            Hexadecimal string representation of the hash.

        Example:
            Comparing with node hash::

                >>> computed = await hasher.hexdigest()
                >>> if computed == node.hash:
                ...     print("Hash matches!")
        """

    @abstractmethod
    async def copy(self) -> Self:
        """Create an independent copy of this hasher.

        Returns a new Hasher instance with the same internal state,
        allowing divergent hash calculations from the same checkpoint.

        Returns:
            New Hasher instance with copied state.

        Example:
            Checkpointing hash calculation::

                >>> checkpoint = await hasher.copy()
                >>> await hasher.update(b"path1")
                >>> await checkpoint.update(b"path2")
        """


type CreateHasher = Callable[[], Awaitable[Hasher]]
"""Factory function type for creating Hasher instances.

Returns an awaitable that produces a new Hasher. Obtained from
Drive.get_hasher_factory(node) or Backend.get_hasher_factory(node).

Example:
    Using hasher factory::

        >>> hasher_factory = await drive.get_hasher_factory(node)
        >>> hasher = await hasher_factory()
        >>> await hasher.update(data)
"""


class WritableFile(metaclass=ABCMeta):
    """Async writable file interface for uploading files to cloud storage.

    Provides sequential and seekable write access for uploading content.
    Returned by Drive.upload_file() and Backend.upload_file() as an
    async context manager.

    Supports resumable uploads through seek() and tell() for recovering
    from interruptions.

    Thread Safety:
        Not thread-safe. Each WritableFile instance should be used from
        a single async task.

    Example:
        Basic upload pattern::

            >>> async with drive.upload_file("file.txt", parent, size=1024) as file:
            ...     with open("local.txt", "rb") as f:
            ...         chunk = f.read(8192)
            ...         while chunk:
            ...             await file.write(chunk)
            ...             chunk = f.read(8192)
            ...     await file.flush()
            ...     node = await file.node()

        Resumable upload::

            >>> async with drive.upload_file("large.bin", parent, size=size) as file:
            ...     try:
            ...         # ... write data ...
            ...         await file.flush()
            ...     except TimeoutError:
            ...         offset = await file.tell()
            ...         await file.seek(offset)
            ...         # Continue from offset
    """

    @abstractmethod
    async def tell(self) -> int:
        """Get the current write position.

        Returns the absolute byte offset of the current write position.
        Used for resumable uploads to determine how much data was successfully
        written before an interruption.

        Returns:
            Current byte offset from the beginning of the file.

        Example:
            Checking upload progress::

                >>> bytes_written = await file.tell()
                >>> print(f"Uploaded {bytes_written}/{total_size} bytes")
        """

    @abstractmethod
    async def seek(self, offset: int) -> int:
        """Seek to the specified position for resuming uploads.

        Moves the write pointer to the given absolute offset from the
        beginning. Used to resume uploads after interruptions.

        Args:
            offset: Absolute byte offset from the beginning.

        Returns:
            The new absolute position.

        Example:
            Resuming upload::

                >>> last_position = await file.tell()
                >>> # ... connection lost ...
                >>> await file.seek(last_position)
                >>> # Continue writing from last position
        """

    @abstractmethod
    async def write(self, chunk: bytes) -> int:
        """Write data to the upload stream.

        Writes the given bytes to the file at the current position.
        May buffer data internally - call flush() to ensure data is sent
        to the server.

        Args:
            chunk: Bytes to write.

        Returns:
            Number of bytes written to the buffer.

        Example:
            Writing with progress tracking::

                >>> total_written = 0
                >>> for chunk in chunks:
                ...     written = await file.write(chunk)
                ...     total_written += written
                ...     print(f"Progress: {total_written} bytes")
        """

    @abstractmethod
    async def flush(self) -> None:
        """Flush buffered data to the server.

        Ensures all buffered data is sent to the cloud storage backend.
        Should be called before closing the file or checking upload status.

        Example:
            Flushing before getting node::

                >>> await file.write(data)
                >>> await file.flush()
                >>> node = await file.node()
        """

    @abstractmethod
    async def node(self) -> Node:
        """Get the uploaded Node.

        Returns the Node instance representing the uploaded file. Should be
        called after flush() to ensure the upload is complete.

        Returns:
            The Node instance for the uploaded file.

        Raises:
            NodeNotFoundError: If upload failed or is incomplete.

        Example:
            Getting uploaded node::

                >>> async with drive.upload_file("doc.pdf", parent, size=size) as file:
                ...     await file.write(data)
                ...     await file.flush()
                ...     uploaded_node = await file.node()
                ...     print(f"Uploaded: {uploaded_node.id}")
        """


class Service(metaclass=ABCMeta):
    """Base interface for all service implementations.

    Defines the minimal contract for Backend implementations and
    middleware. All services must declare their API version for
    compatibility checking.

    Implementations:
        Subclass Backend rather than this class directly.
    """

    @property
    @abstractmethod
    def api_version(self) -> int:
        """Get the API version implemented by this service.

        Must return the API version number that this implementation adheres
        to. Used during drive creation to ensure compatibility between
        the core library and service implementations.

        Returns:
            API version number as an integer.

        Raises:
            InvalidServiceError: During drive creation if version mismatches.

        Example:
            Implementing api_version::

                >>> class MyBackend(Backend):
                ...     @property
                ...     def api_version(self) -> int:
                ...         return 6
        """


type CreateService[T: Service] = Callable[[], AbstractAsyncContextManager[T]]
"""Factory type for creating Service instances as async context managers.

A callable that returns an async context manager yielding a Service
instance. Used for dependency injection during drive creation.

Type Parameters:
    T: The specific Service subclass (Backend).

Example:
    Implementing a service factory::

        >>> @asynccontextmanager
        >>> async def create_my_service() -> AsyncIterator[MyBackend]:
        ...     service = MyBackend()
        ...     try:
        ...         await service.initialize()
        ...         yield service
        ...     finally:
        ...         await service.cleanup()
"""


type CreateServiceMiddleware[T: Service] = Callable[[T], AbstractAsyncContextManager[T]]
"""Middleware factory type for wrapping Service instances.

A callable that takes a Service instance and returns an async context
manager yielding a wrapped Service. Used for adding cross-cutting concerns
like logging, caching, or rate limiting.

Type Parameters:
    T: The specific Service subclass being wrapped.

Example:
    Implementing logging middleware::

        >>> @asynccontextmanager
        >>> async def logging_middleware(
        ...     service: Backend,
        ... ) -> AsyncIterator[Backend]:
        ...     print(f"Service started: {service}")
        ...     try:
        ...         yield service
        ...     finally:
        ...         print(f"Service stopped: {service}")
"""


class Backend(Service, metaclass=ABCMeta):
    """Backend implementation interface for cloud storage operations.

    Backend defines the contract for interacting with cloud storage
    APIs. Implementations provide the actual I/O operations for a specific
    cloud provider (e.g., Google Drive, Dropbox, OneDrive).

    Implementations must:
        - Provide all abstract methods
        - Return api_version = 6
        - Handle authentication appropriately
        - Support async context manager protocol
        - Raise appropriate exceptions on errors

    Used With:
        Passed to create_drive() together with a cache path.

    Thread Safety:
        Implementations should handle concurrent operations safely.

    Example:
        Basic implementation structure::

            >>> class MyCloudBackend(Backend):
            ...     @property
            ...     def api_version(self) -> int:
            ...         return 6
            ...
            ...     async def get_root(self) -> Node:
            ...         # Fetch root from API
            ...         pass
            ...
            ...     # ... implement other abstract methods ...

        Using with create_drive::

            >>> @asynccontextmanager
            >>> async def create_my_service():
            ...     service = MyCloudBackend(credentials)
            ...     try:
            ...         yield service
            ...     finally:
            ...         await service.close()
            >>>
            >>> async with create_drive(
            ...     backend=create_my_service,
            ...     cache_path="/path/to/cache.sqlite",
            ... ) as drive:
            ...     await drive.sync()
    """

    @abstractmethod
    async def get_initial_cursor(self) -> str:
        """Get the initial checkpoint cursor for change tracking.

        Returns a cursor representing the current state of the cloud drive
        before any changes are tracked. Used as the starting point for the
        first sync operation.

        Returns:
            Initial cursor string. Format is implementation-specific.

        Example:
            Implementation pattern::

                >>> async def get_initial_cursor(self) -> str:
                ...     # For some APIs, this might be a special token
                ...     return "start_token"
                ...     # For others, might be current timestamp
                ...     return str(int(time.time()))
        """

    @abstractmethod
    async def get_root(self) -> Node:
        """Fetch the root directory node from the cloud storage.

        Retrieves the root directory's metadata from the cloud API and
        returns it as a Node instance. Drive stores it in its local cache
        after the first sync.

        Returns:
            Node representing the root directory.

        Raises:
            AuthenticationError: If not authenticated.

        Example:
            Implementation pattern::

                >>> async def get_root(self) -> Node:
                ...     response = await self.api_client.get("/root")
                ...     return self._api_response_to_node(response)
        """

    @abstractmethod
    def get_changes(
        self,
        cursor: str,
    ) -> AsyncIterator[tuple[list[ChangeAction], str]]:
        """Fetch incremental changes from the cloud storage.

        Returns an async iterator that yields pages of changes since the
        given cursor. Each iteration provides a list of changes and the
        cursor for the next page.

        This method implements polling or pagination to retrieve all changes
        since the given checkpoint.

        Args:
            cursor: Starting cursor from previous sync or get_initial_cursor().

        Yields:
            Tuples of (changes, next_cursor) where:
                - changes: List of ChangeAction (additions/updates/removals)
                - next_cursor: Cursor for the next page or next sync

        Raises:
            StaleCursorError: If the source can no longer continue from
                *cursor*. Drive then rebuilds the source's cache from the
                initial cursor.

        Example:
            Implementation pattern::

                >>> async def get_changes(
                ...     self,
                ...     cursor: str,
                ... ) -> AsyncIterator[tuple[list[ChangeAction], str]]:
                ...     while True:
                ...         response = await self.api_client.get_changes(cursor)
                ...         changes = [self._convert_change(c) for c in response.changes]
                ...         yield changes, response.next_cursor
                ...         if not response.has_more:
                ...             break
                ...         cursor = response.next_cursor

            Usage in sync::

                >>> async for changes, next_cursor in service.get_changes(cursor):
                ...     await cache.apply_changes(changes, next_cursor)
        """

    @abstractmethod
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None,
        new_name: str | None,
    ) -> Node:
        """Relocate or rename a node.

        Performs one or more operations on the node:
        - Rename: Set new_name to change the node's name
        - Move: Set new_parent to move to a different directory

        At least one of the optional parameters must be non-None.

        Args:
            node: The Node to modify.
            new_parent: New parent directory, or None to keep current parent.
            new_name: New name, or None to keep current name.

        Returns:
            New Node instance reflecting the changes.

        Raises:
            AuthenticationError: If not authenticated.
            ValueError: If all optional parameters are None.
        """

    @abstractmethod
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        """Delete a node.

        Soft-deletes by default (best-effort per backend). Pass permanent=True
        for a hard delete that cannot be undone.

        Args:
            node: The Node to delete.
            permanent: If True, permanently delete. If False (default),
                soft-delete (e.g. move to trash).

        Raises:
            AuthenticationError: If not authenticated.
        """

    @abstractmethod
    async def restore(self, node: Node) -> Node:
        """Restore a trashed node.

        Args:
            node: The trashed Node to restore.

        Returns:
            Updated Node instance reflecting the restored state.

        Raises:
            NotImplementedError: If backend doesn't support restore.
        """

    @abstractmethod
    async def purge_trash(self) -> None:
        """Permanently delete all trashed items.

        Empties the trash bin, permanently removing all trashed nodes.
        This operation cannot be undone.

        Raises:
            AuthenticationError: If not authenticated.

        Example:
            Implementation pattern::

                >>> async def purge_trash(self) -> None:
                ...     await self.api_client.empty_trash()
        """

    @abstractmethod
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool,
        private: PrivateDict | None,
    ) -> Node:
        """Create a new directory.

        Creates a directory with the given name under the specified parent.
        Behavior when the directory exists depends on exist_ok.

        Args:
            name: Name for the new directory.
            parent: Parent directory Node.
            exist_ok: If True, don't raise error if directory exists.
                If False, raise NodeExistsError if directory exists.
            private: Optional service-specific metadata.

        Returns:
            Node representing the created (or existing) directory.

        Raises:
            AuthenticationError: If not authenticated.
            NodeExistsError: If exist_ok=False and directory exists.

        Example:
            Implementation pattern::

                >>> async def create_directory(
                ...     self,
                ...     name: str,
                ...     parent: Node,
                ...     *,
                ...     exist_ok: bool,
                ...     private: PrivateDict | None,
                ... ) -> Node:
                ...     if not exist_ok:
                ...         existing = await self._find_child(parent.id, name)
                ...         if existing:
                ...             raise NodeExistsError(existing)
                ...     response = await self.api_client.create_folder(
                ...         name=name,
                ...         parent_id=parent.id,
                ...     )
                ...     return self._api_response_to_node(response)
        """

    @abstractmethod
    def download_file(self, node: Node) -> AbstractAsyncContextManager[ReadableFile]:
        """Create a readable stream for downloading a file.

        Returns an async context manager that yields a ReadableFile for
        streaming the file content from cloud storage.

        Args:
            node: The file Node to download.

        Returns:
            Async context manager yielding ReadableFile.

        Raises:
            AuthenticationError: If not authenticated.
            ValueError: If node is a directory.

        Example:
            Implementation pattern::

                >>> @asynccontextmanager
                >>> async def download_file(
                ...     self,
                ...     node: Node,
                ... ) -> AsyncIterator[ReadableFile]:
                ...     stream = await self.api_client.download(node.id)
                ...     try:
                ...         yield MyReadableFile(stream, node)
                ...     finally:
                ...         await stream.close()
        """

    @abstractmethod
    def upload_file(
        self,
        name: str,
        parent: Node,
        *,
        size: int | None,
        mime_type: str | None,
        media_info: MediaInfo | None,
        private: PrivateDict | None,
    ) -> AbstractAsyncContextManager[WritableFile]:
        """Create a writable stream for uploading a file.

        Returns an async context manager that yields a WritableFile for
        streaming content to cloud storage.

        Args:
            name: Name for the uploaded file.
            parent: Parent directory Node.
            size: File size in bytes, or None if unknown.
            mime_type: MIME type string, or None for default.
            media_info: Optional media metadata for images/videos.
            private: Optional service-specific metadata.

        Returns:
            Async context manager yielding WritableFile.

        Raises:
            AuthenticationError: If not authenticated.
            NodeExistsError: If file with same name already exists.

        Example:
            Implementation pattern::

                >>> @asynccontextmanager
                >>> async def upload_file(
                ...     self,
                ...     name: str,
                ...     parent: Node,
                ...     *,
                ...     size: int | None,
                ...     mime_type: str | None,
                ...     media_info: MediaInfo | None,
                ...     private: PrivateDict | None,
                ... ) -> AsyncIterator[WritableFile]:
                ...     stream = await self.api_client.upload_start(
                ...         name=name,
                ...         parent_id=parent.id,
                ...         size=size,
                ...     )
                ...     try:
                ...         yield MyWritableFile(stream)
                ...     finally:
                ...         await stream.finalize()
        """

    @abstractmethod
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        """Get a factory for creating hash calculators for a file node.

        Returns a factory function that creates Hasher instances matching
        ``node.hash_algorithm`` (e.g., MD5, SHA-1, SHA-256). Algorithms may
        differ between files of the same service.

        The returned factory may be sent to a process pool, so it must be
        picklable (a module-level function or a ``functools.partial`` of one).

        Args:
            node: The file node whose checksum will be verified.

        Returns:
            Factory function for creating Hasher instances.

        Raises:
            UnsupportedHashAlgorithmError: If the service cannot compute
                ``node.hash_algorithm``. Services must not fall back to
                another algorithm.

        Example:
            Implementation pattern::

                >>> async def get_hasher_factory(self, node: Node) -> CreateHasher:
                ...     if node.hash_algorithm != "md5":
                ...         raise UnsupportedHashAlgorithmError(node)
                ...     return create_md5_hasher
        """

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Check if the service is authenticated and ready for operations.

        Checks cached authentication state without performing I/O if possible.
        Should verify that credentials/tokens are present and not obviously
        expired.

        Returns:
            True if authenticated and ready, False otherwise.

        Example:
            Implementation pattern::

                >>> async def is_authenticated(self) -> bool:
                ...     return self.access_token is not None
        """

    @abstractmethod
    async def authenticate(self) -> None:
        """Authenticate the service for use.

        Establishes authentication using implementation-specific mechanisms
        (OAuth tokens, API keys, etc.). Implementations must manage their
        own credential storage and token refresh.

        This method is NOT interactive. OAuth implementations should handle
        token exchange separately before calling this method.

        Raises:
            AuthenticationError: If authentication fails.

        Example:
            Implementation pattern::

                >>> async def authenticate(self) -> None:
                ...     if not self.credentials:
                ...         raise AuthenticationError("No credentials configured")
                ...     response = await self.api_client.validate_token(
                ...         self.credentials.access_token
                ...     )
                ...     if not response.valid:
                ...         raise AuthenticationError("Invalid credentials")
        """


type BackendFactory = CreateService[Backend]
"""Factory type for creating Backend instances.

See CreateService for details.
"""


type BackendMiddleware = CreateServiceMiddleware[Backend]
"""Middleware factory type for wrapping Backend instances.

See CreateServiceMiddleware for details.
"""


class Drive(metaclass=ABCMeta):
    """High-level interface for cloud drive operations.

    Drive is the main user-facing API for interacting with cloud storage.
    It combines a Backend (cloud API) with a local metadata cache
    to provide efficient, feature-rich operations.

    Key Features:
        - Path-based and ID-based node access
        - Directory traversal and tree walking
        - File upload/download with resumable support
        - Node manipulation (create, move, rename, delete)
        - Change synchronization
        - Trash management

    Usage Pattern:
        Create Drive instances using create_drive() as an async context manager.
        The Drive interface ensures proper initialization and cleanup.

    Authentication:
        Most operations require authentication. Use is_authenticated() to check
        and authenticate() to establish credentials.

    Thread Safety:
        Drive operations are async-safe but instances should not be shared
        across concurrent tasks without external synchronization.

    Example:
        Basic usage pattern::

            >>> async with create_drive(
            ...     backend=create_backend,
            ...     cache_path="/path/to/cache.sqlite",
            ... ) as drive:
            ...     # Check authentication
            ...     if not await drive.is_authenticated():
            ...         await drive.authenticate()
            ...
            ...     # Sync to get latest changes
            ...     async for change in drive.sync():
            ...         print(f"Change: {change}")
            ...
            ...     # Navigate filesystem
            ...     root = await drive.get_root()
            ...     children = await drive.get_children(root)
            ...
            ...     # Upload a file
            ...     async with drive.upload_file("doc.txt", root, size=1024) as f:
            ...         await f.write(b"Hello World")
            ...         await f.flush()
            ...         node = await f.node()
    """

    @abstractmethod
    async def get_root(self) -> Node:
        """Get the root directory node.

        Returns the root node of the drive's directory structure.

        Returns:
            Node representing the root directory.

        Raises:
            NodeNotFoundError: If not yet synced (cache empty).

        Example:
            Getting root and listing contents::

                >>> root = await drive.get_root()
                >>> print(f"Root ID: {root.id}")
                >>> children = await drive.get_children(root)
        """

    @abstractmethod
    async def get_node_by_id(self, node_id: str) -> Node:
        """Get a node by its unique identifier.

        Fast lookup using the node's ID, retrieved from cache.

        Args:
            node_id: The node's unique ID.

        Returns:
            The Node with the given ID.

        Raises:
            NodeNotFoundError: If node not found in cache.

        Example:
            Getting node by ID::

                >>> node = await drive.get_node_by_id("abc123")
                >>> print(f"Found: {node.name}")
        """

    @abstractmethod
    async def get_node_by_path(self, path: PurePath) -> Node:
        """Get a node by its absolute path.

        Resolves the path through the cached directory structure.

        Args:
            path: Absolute path to the node (must start with "/").

        Returns:
            The Node at the given path.

        Raises:
            NodeNotFoundError: If path does not exist.
            ValueError: If path is not absolute.

        Example:
            Getting node by path::

                >>> from pathlib import PurePath
                >>> node = await drive.get_node_by_path(PurePath("/docs/file.txt"))
                >>> print(f"Size: {node.size} bytes")
        """

    @abstractmethod
    async def resolve_path(self, node: Node) -> PurePath:
        """Resolve the absolute path of a node.

        Constructs the full path by walking up the parent chain.

        Args:
            node: The Node to resolve.

        Returns:
            Absolute PurePath to the node.

        Example:
            Resolving node path::

                >>> node = await drive.get_node_by_id("abc123")
                >>> path = await drive.resolve_path(node)
                >>> print(f"Path: {path}")
        """

    @abstractmethod
    async def get_child_by_name(
        self,
        name: str,
        parent: Node,
    ) -> Node:
        """Find a child node by name under a parent.

        Searches for a direct child with the given name.

        Args:
            name: The child's name to find.
            parent: The parent Node to search under.

        Returns:
            The child Node with matching name.

        Raises:
            NodeNotFoundError: If no child with that name exists.

        Example:
            Finding child by name::

                >>> root = await drive.get_root()
                >>> docs = await drive.get_child_by_name("Documents", root)
        """

    @abstractmethod
    async def get_children(self, parent: Node) -> list[Node]:
        """Get all direct children of a directory.

        Returns both files and subdirectories under the parent.

        Args:
            parent: The parent directory Node.

        Returns:
            List of child Nodes (may be empty).

        Example:
            Listing directory contents::

                >>> root = await drive.get_root()
                >>> children = await drive.get_children(root)
                >>> for child in children:
                ...     type_ = "DIR" if child.is_directory else "FILE"
                ...     print(f"{type_}: {child.name}")
        """

    @abstractmethod
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        """Get all nodes in the trash.

        Args:
            flatten: If False (default), exclude nodes whose parent is
                also trashed. If True, return all trashed nodes.

        Returns:
            List of trashed Nodes (may be empty).

        Example:
            Listing trash contents::

                >>> trashed = await drive.get_trashed_nodes()
                >>> for node in trashed:
                ...     print(f"Trashed: {node.name}")

            Getting all trashed nodes including nested::

                >>> all_trashed = await drive.get_trashed_nodes(flatten=True)
        """

    def find_nodes(self) -> NodeQueryBuilder:
        """Start a search over the whole drive.

        Chain conditions, then await all() to retrieve nodes or summarize()
        to count nodes and sum file sizes. Non-trashed nodes are matched
        unless the builder says otherwise.

        Returns:
            A NodeQueryBuilder bound to query_nodes() and summarize_nodes().

        Example:
            Finding all Python files::

                >>> nodes = await drive.find_nodes().name_matches(r"\\.py$").all()
                >>> for node in nodes:
                ...     print(f"Python file: {node.name}")

            Finding empty files under a directory::

                >>> nodes = await drive.find_nodes().descendant_of(node).size(max=0).all()
        """
        return NodeQueryBuilder(self.query_nodes, summarize=self.summarize_nodes)

    @abstractmethod
    async def query_nodes(self, query: NodeQuery) -> list[Node]:
        """Find nodes matching all conditions in a query.

        Prefer find_nodes(), which builds the query fluently.

        Args:
            query: Search conditions. See NodeQuery.

        Returns:
            List of matching Nodes in no particular order (may be empty).

        Raises:
            NodeNotFoundError: If query.ancestor_id is not a known node.
        """

    @abstractmethod
    async def summarize_nodes(self, query: NodeQuery) -> NodeSummary:
        """Count matching nodes and sum matching file sizes.

        Directories contribute to count but add no size. Empty matches return
        zero for both fields. Uses the same filters as query_nodes().

        Raises:
            ValueError: If query.limit is set.
            NodeNotFoundError: If query.ancestor_id is not a known node.
        """

    @abstractmethod
    def walk(
        self,
        node: Node,
        *,
        include_trashed: bool = False,
    ) -> AsyncIterator[tuple[Node, list[Node], list[Node]]]:
        """Recursively traverse directory tree from a starting node.

        Similar to os.walk(), yields directory information in depth-first order.

        Args:
            node: Starting directory Node for traversal.
            include_trashed: If True, include trashed nodes in traversal.

        Yields:
            Tuples of (directory, subdirectories, files) where:
                - directory: Current directory Node being visited
                - subdirectories: List of direct subdirectory Nodes
                - files: List of direct file Nodes

        Example:
            Walking entire drive::

                >>> root = await drive.get_root()
                >>> async for directory, subdirs, files in drive.walk(root):
                ...     print(f"Directory: {directory.name}")
                ...     for file in files:
                ...         print(f"  File: {file.name} ({file.size} bytes)")

            Finding all images::

                >>> root = await drive.get_root()
                >>> async for directory, _, files in drive.walk(root):
                ...     for file in files:
                ...         if file.is_image:
                ...             path = await drive.resolve_path(file)
                ...             print(f"Image: {path}")
        """

    @abstractmethod
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool = False,
    ) -> Node:
        """Create a new directory.

        Creates a directory with the given name under the parent.

        Args:
            name: Name for the new directory.
            parent: Parent directory Node where directory will be created.
            exist_ok: If True, don't raise error if directory already exists.

        Returns:
            Node representing the created (or existing if exist_ok) directory.

        Raises:
            AuthenticationError: If not authenticated.
            NodeExistsError: If exist_ok=False and directory already exists.
            ValueError: If name contains invalid characters (/ or \\).

        Example:
            Creating a directory::

                >>> root = await drive.get_root()
                >>> docs = await drive.create_directory("Documents", root)
                >>> print(f"Created: {docs.name} ({docs.id})")

            Creating with exist_ok::

                >>> docs = await drive.create_directory(
                ...     "Documents",
                ...     root,
                ...     exist_ok=True,
                ... )
        """

    @abstractmethod
    def download_file(self, node: Node) -> AbstractAsyncContextManager[ReadableFile]:
        """Download a file from the cloud drive.

        Returns an async context manager that yields a ReadableFile for
        streaming the file content.

        Args:
            node: The file Node to download.

        Returns:
            Async context manager yielding ReadableFile.

        Raises:
            AuthenticationError: If not authenticated.
            ValueError: If node is a directory.

        Example:
            Downloading a file::

                >>> node = await drive.get_node_by_path(PurePath("/file.txt"))
                >>> async with drive.download_file(node) as file:
                ...     content = await file.read(1024)
                ...     while content:
                ...         process(content)
                ...         content = await file.read(1024)

            Using higher-level helper::

                >>> from wcpan.drive.client.lib import download_file_to_local
                >>> from pathlib import Path
                >>> local_path = await download_file_to_local(
                ...     drive,
                ...     node,
                ...     Path("/downloads"),
                ... )
        """

    @abstractmethod
    def upload_file(
        self,
        name: str,
        parent: Node,
        *,
        size: int | None = None,
        mime_type: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> AbstractAsyncContextManager[WritableFile]:
        """Upload a file to the cloud drive.

        Returns an async context manager that yields a WritableFile for
        streaming content to the cloud.

        Args:
            name: Name for the uploaded file.
            parent: Parent directory Node where file will be uploaded.
            size: File size in bytes, or None if unknown.
            mime_type: MIME type string, or None for default.
            media_info: Optional MediaInfo for images/videos.

        Returns:
            Async context manager yielding WritableFile.

        Raises:
            AuthenticationError: If not authenticated.
            NodeExistsError: If file with same name already exists.
            ValueError: If name contains invalid characters (/ or \\).

        Example:
            Uploading a file::

                >>> root = await drive.get_root()
                >>> async with drive.upload_file("doc.txt", root, size=11) as file:
                ...     await file.write(b"Hello World")
                ...     await file.flush()
                ...     node = await file.node()
                ...     print(f"Uploaded: {node.id}")

            Using higher-level helper::

                >>> from wcpan.drive.client.lib import upload_file_from_local
                >>> from pathlib import Path
                >>> node = await upload_file_from_local(
                ...     drive,
                ...     Path("/local/file.jpg"),
                ...     parent,
                ...     mime_type="image/jpeg",
                ... )
        """

    @abstractmethod
    async def purge_trash(self) -> None:
        """Permanently delete all items in the trash.

        Empties the trash bin, permanently removing all trashed nodes.
        This operation cannot be undone.

        Raises:
            AuthenticationError: If not authenticated.

        Example:
            Purging trash::

                >>> await drive.purge_trash()
                >>> trashed = await drive.get_trashed_nodes()
                >>> assert len(trashed) == 0
        """

    @abstractmethod
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
    ) -> Node:
        """Relocate or rename a node.

        At least one parameter must be non-None.

        Args:
            node: The Node to modify.
            new_parent: New parent directory, or None to keep current.
            new_name: New name, or None to keep current.

        Returns:
            New Node instance with updated properties.

        Raises:
            AuthenticationError: If not authenticated.
            ValueError: If all parameters are None, or if trying to move
                root node, or if new_name contains invalid characters.
        """

    @abstractmethod
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        """Delete a node.

        Soft-deletes by default (best-effort per backend). Pass permanent=True
        for a hard delete that cannot be undone.

        Args:
            node: The Node to delete.
            permanent: If True, permanently delete. If False (default),
                soft-delete (e.g. move to trash).

        Raises:
            AuthenticationError: If not authenticated.
        """

    @abstractmethod
    async def restore(self, node: Node) -> Node:
        """Restore a trashed node.

        Args:
            node: The trashed Node to restore.

        Returns:
            Updated Node instance reflecting the restored state.

        Raises:
            NotImplementedError: If backend doesn't support restore.
        """

    @abstractmethod
    def sync(self) -> AsyncIterator[ChangeAction]:
        """Synchronize the local cache with cloud storage.

        Fetches changes from the cloud and updates the local cache.
        This is the ONLY operation that modifies the cache.

        Should be called periodically or before operations that require
        up-to-date metadata.

        Yields:
            ChangeAction instances for each change detected.

        Raises:
            AuthenticationError: If not authenticated.

        Example:
            Basic sync::

                >>> async for change in drive.sync():
                ...     if change[0]:  # RemoveAction
                ...         print(f"Removed: {change[1]}")
                ...     else:  # UpdateAction
                ...         print(f"Updated: {change[1].name}")

            Sync with type guards::

                >>> from wcpan.drive.client.lib import is_remove, is_update
                >>> async for change in drive.sync():
                ...     if is_remove(change):
                ...         removed, node_id = change
                ...         handle_removal(node_id)
                ...     elif is_update(change):
                ...         updated, node = change
                ...         handle_update(node)
        """

    @abstractmethod
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        """Get a factory for creating hash calculators for a given node.

        Returns a factory function compatible with ``node.hash_algorithm``,
        as computed by the backend that owns the node.

        Args:
            node: The file node whose checksum will be verified.

        Returns:
            Factory function for creating Hasher instances.

        Raises:
            UnsupportedHashAlgorithmError: If the owning backend cannot
                compute the node's hash algorithm.

        Example:
            Computing file hash::

                >>> hasher_factory = await drive.get_hasher_factory(node)
                >>> hasher = await hasher_factory()
                >>> with open("file.txt", "rb") as f:
                ...     while chunk := f.read(8192):
                ...         await hasher.update(chunk)
                >>> file_hash = await hasher.hexdigest()
        """

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Check if the drive is authenticated and ready for operations.

        Returns True if authenticated, False otherwise. Does not perform
        I/O - checks cached authentication state.

        Returns:
            True if authenticated, False otherwise.

        Example:
            Checking before operations::

                >>> if await drive.is_authenticated():
                ...     await drive.sync()
                ... else:
                ...     print("Not authenticated")
        """

    @abstractmethod
    async def authenticate(self) -> None:
        """Authenticate the drive for use.

        Establishes authentication credentials. The specific mechanism
        depends on the underlying Backend implementation.

        This method is NOT interactive. OAuth implementations should handle
        token exchange separately.

        Raises:
            AuthenticationError: If authentication fails.

        Example:
            Authenticating before use::

                >>> async with create_drive(...) as drive:
                ...     await drive.authenticate()
                ...     await drive.sync()

            Conditional authentication::

                >>> if not await drive.is_authenticated():
                ...     await drive.authenticate()
                >>> await drive.upload_file(...)
        """


@dataclass(frozen=True, kw_only=True)
class SourceConfig:
    """Configuration for a single storage backend source.

    Used with create_multi_drive() to configure one or more storage backends.
    In single-source mode, paths are unchanged. In multi-source mode
    (2+ sources), each source's paths are prefixed with its name
    (e.g., /google/docs/file.txt).

    Attributes:
        name: Source name used as path prefix in multi-source mode.
        backend: Factory function returning a Backend context manager.
        cache_path: Path to the SQLite file that caches this source's
            metadata. Relative paths resolve against the working directory.
    """

    name: str
    backend: BackendFactory
    cache_path: str
