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
    "ChangeAction",
    "CreateFileService",
    "CreateFileServiceMiddleware",
    "CreateSnapshotService",
    "CreateSnapshotServiceMiddleware",
    "FileService",
    "Hasher",
    "MediaInfo",
    "Node",
    "PrivateDict",
    "ReadableFile",
    "RemoveAction",
    "UpdateAction",
    "WritableFile",
)


type PrivateDict = dict[str, str]


@dataclass(frozen=True, kw_only=True)
class Node:
    id: str
    parent_id: str | None
    name: str
    is_directory: bool
    is_trashed: bool
    ctime: datetime
    mtime: datetime
    mime_type: str
    hash: str
    size: int
    is_image: bool
    is_video: bool
    width: int
    height: int
    ms_duration: int
    private: PrivateDict | None


type RemoveAction = tuple[Literal[True], str]
type UpdateAction = tuple[Literal[False], Node]
type ChangeAction = RemoveAction | UpdateAction


class MediaInfo:
    @classmethod
    def image(cls, width: int, height: int) -> Self:
        return cls(is_image=True, width=width, height=height)

    @classmethod
    def video(cls, width: int, height: int, ms_duration: int) -> Self:
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
        return self._is_image

    @property
    def is_video(self) -> bool:
        return self._is_video

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def ms_duration(self) -> int:
        return self._ms_duration


class ReadableFile(AsyncIterable[bytes], metaclass=ABCMeta):
    """
    An async readable file interface.
    """

    @abstractmethod
    async def read(self, length: int) -> bytes:
        """
        Read at most `length` bytes.
        """

    @abstractmethod
    async def seek(self, offset: int) -> int:
        """
        Seek to `offset` position. Always starts from the begining.
        """

    @abstractmethod
    async def node(self) -> Node:
        """
        Get the node being read.
        """


class Hasher(metaclass=ABCMeta):
    """
    Hash calculator.
    """

    @abstractmethod
    async def update(self, data: bytes) -> None:
        """
        Put `data` into the stream.
        """

    @abstractmethod
    async def digest(self) -> bytes:
        """
        Get raw digest.
        """

    @abstractmethod
    async def hexdigest(self) -> str:
        """
        Get hex digest.
        """

    @abstractmethod
    async def copy(self) -> Self:
        """
        Return a copy to self.
        """


type CreateHasher = Callable[[], Awaitable[Hasher]]


class WritableFile(metaclass=ABCMeta):
    """
    An async writable file interface.
    """

    @abstractmethod
    async def tell(self) -> int:
        """
        Get current position.
        """

    @abstractmethod
    async def seek(self, offset: int) -> int:
        """
        Seek to `offset` position. Always starts from the begining.
        """

    @abstractmethod
    async def write(self, chunk: bytes) -> int:
        """
        Writes `chunk` to the stream.
        Returns bytes writen to buffer.
        """

    @abstractmethod
    async def flush(self) -> None:
        """
        Flush buffer.
        """

    @abstractmethod
    async def node(self) -> Node:
        """
        Get the wrote node.

        Raises:
        NodeNotFoundError if failed.
        """


class Service(metaclass=ABCMeta):
    @property
    @abstractmethod
    def api_version(self) -> int:
        """
        Get competible API version for this class.
        """


type CreateService[T: Service] = Callable[[], AbstractAsyncContextManager[T]]
type CreateServiceMiddleware[T: Service] = Callable[[T], AbstractAsyncContextManager[T]]


class FileService(Service, metaclass=ABCMeta):
    """
    Provides actions to cloud drives.
    """

    @abstractmethod
    async def get_initial_cursor(self) -> str:
        """
        Get the initial check point.
        """

    @abstractmethod
    async def get_root(self) -> Node:
        """
        Fetch the root node.
        """

    @abstractmethod
    def get_changes(
        self,
        cursor: str,
    ) -> AsyncIterator[tuple[list[ChangeAction], str]]:
        """
        Fetch changes starts from `cursor`.

        Will be used like this:
        ```
        async for changes, next_cursor in self.fetch_changes('...'):
            ...
        ```
        So you should yield a page for every iteration.
        """

    @abstractmethod
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None,
        new_name: str | None,
        trashed: bool | None,
    ) -> Node:
        """
        Changes the state of the given node:
        1. Rename the node.
        2. Move the node to another directory.
        3. Put the node to trash or restore it from trash.

        `node` is the node to be modified.

        `new_parent` is the new parent directory. `None` means don't move the node.

        `new_name` is the new node name. `None` means don't rename the node.

        `trashed` sets to `True` to put the node to trash. Sets to `False` will
        restore it. `None` means don't change trash state.
        """

    @abstractmethod
    async def delete(self, node: Node) -> None:
        """
        Permanently delete the node.
        """

    @abstractmethod
    async def purge_trash(self) -> None:
        """
        Purge everything in the trash.

        Should raise exception if failed.
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
        """
        Create a directory.

        `name` will be the name of the directory.

        `parent` should be a directory you want to put this directory in.

        If `exist_ok` is `False`, you should not create the directory if it is
        already exists, and raise an exception.

        `private` is an optional metadata, you can decide how to place this for
        each services.

        Will return the created node.
        """

    @abstractmethod
    def download_file(self, node: Node) -> AbstractAsyncContextManager[ReadableFile]:
        """
        Download the node.

        Will return a `ReadableFile` which is a file-like object.
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
        """
        Upload a file.

        `name` is the name of the uploaded file.

        `parent` is the parent directory where this file upload to.

        `size` can be `None`, for cases that the file size is unavailable.
        e.g. The uploading file is from a stream.

        `mime_type`, `media_info` and `private` are optional. It is your choice
        to decide how to place these properties.
        """

    @abstractmethod
    async def get_hasher_factory(self) -> CreateHasher:
        """
        Get a hash calculator factory.
        """

    @abstractmethod
    async def is_authorized(self) -> bool:
        """
        Is OAuth 2.0 authorized.
        """

    @abstractmethod
    async def get_oauth_url(self) -> str:
        """
        Get OAuth 2.0 URL.
        """

    @abstractmethod
    async def set_oauth_token(self, token: str) -> None:
        """
        Set OAuth 2.0 token.
        """


type CreateFileService = CreateService[FileService]
type CreateFileServiceMiddleware = CreateServiceMiddleware[FileService]


class SnapshotService(Service, metaclass=ABCMeta):
    """
    Provides actions to cache file metadata.
    """

    @abstractmethod
    async def get_root(self) -> Node:
        """
        Get root directory as Node.
        """

    @abstractmethod
    async def set_root(self, node: Node) -> None:
        """
        Set root directory to the snapshot.
        """

    @abstractmethod
    async def get_current_cursor(self) -> str:
        """
        Get the current cursor. If no cursor present (e.g. the first run),
        should return an empty string.
        """

    @abstractmethod
    async def get_node_by_id(self, node_id: str) -> Node:
        """
        Get node by ID.
        """

    @abstractmethod
    async def get_node_by_path(self, path: PurePath) -> Node:
        """
        Resolve node by file path.
        """

    @abstractmethod
    async def resolve_path_by_id(self, node_id: str) -> PurePath:
        """
        Resolve absolute path by ID.
        """

    @abstractmethod
    async def get_child_by_name(self, name: str, parent_id: str) -> Node:
        """
        Get a child under the given parent by name.
        """

    @abstractmethod
    async def get_children_by_id(self, parent_id: str) -> list[Node]:
        """
        Get first-level children under a node.
        """

    @abstractmethod
    async def get_trashed_nodes(self) -> list[Node]:
        """
        Get trashed nodes.
        """

    @abstractmethod
    async def apply_changes(
        self,
        changes: list[ChangeAction],
        cursor: str,
    ) -> None:
        """
        Apply the given changes to the snapshot.
        """

    @abstractmethod
    async def find_nodes_by_regex(self, pattern: str) -> list[Node]:
        """
        Find node by regex.
        """


type CreateSnapshotService = CreateService[SnapshotService]
type CreateSnapshotServiceMiddleware = CreateServiceMiddleware[SnapshotService]


class Drive(metaclass=ABCMeta):
    """
    Interact with the drive.
    """

    @abstractmethod
    async def get_root(self) -> Node:
        """Get the root node."""

    @abstractmethod
    async def get_node_by_id(self, node_id: str) -> Node:
        """Get node by node id."""

    @abstractmethod
    async def get_node_by_path(self, path: PurePath) -> Node:
        """Get node by absolute path."""

    @abstractmethod
    async def resolve_path(self, node: Node) -> PurePath:
        """Resolve absolute path of the node."""

    @abstractmethod
    async def get_child_by_name(
        self,
        name: str,
        parent: Node,
    ) -> Node:
        """Get node by given name and parent."""

    @abstractmethod
    async def get_children(self, parent: Node) -> list[Node]:
        """Get the child node list of given node."""

    @abstractmethod
    async def get_trashed_nodes(self, flatten: bool = False) -> list[Node]:
        """Get trashed node list."""

    @abstractmethod
    async def find_nodes_by_regex(self, pattern: str) -> list[Node]:
        """Find nodes by name."""

    @abstractmethod
    def walk(
        self,
        node: Node,
        *,
        include_trashed: bool = False,
    ) -> AsyncIterator[tuple[Node, list[Node], list[Node]]]:
        """Traverse nodes from given node."""

    @abstractmethod
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool = False,
    ) -> Node:
        """Create a directory."""

    @abstractmethod
    def download_file(self, node: Node) -> AbstractAsyncContextManager[ReadableFile]:
        """Download file."""

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
        """Upload file."""

    @abstractmethod
    async def purge_trash(self) -> None:
        """Purge everything in trash."""

    @abstractmethod
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None = None,
        new_name: str | None = None,
        trashed: bool | None = None,
    ) -> Node:
        """Move or rename or trash the node."""

    @abstractmethod
    async def delete(self, node: Node) -> None:
        """Permanently delete the node."""

    @abstractmethod
    def sync(self) -> AsyncIterator[ChangeAction]:
        """Synchronize the snapshot.

        This is the ONLY function which will modify the snapshot.
        """

    @abstractmethod
    async def get_hasher_factory(self) -> CreateHasher:
        """Get a Hasher instance for checksum calculation."""

    @abstractmethod
    async def is_authorized(self) -> bool:
        """Is the drive authorized."""

    @abstractmethod
    async def get_oauth_url(self) -> str:
        """Get OAuth 2.0 URL"""

    @abstractmethod
    async def set_oauth_token(self, token: str) -> None:
        """Set OAuth 2.0 token"""
