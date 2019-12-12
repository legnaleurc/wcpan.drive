from .types import Node


class DriveError(Exception):
    pass


class InvalidRemoteDriverError(DriveError):
    pass


class InvalidMiddlewareError(DriveError):
    pass


# NOTE *MUST* be picklable
class CacheError(DriveError):

    def __init__(self, message: str) -> None:
        super(CacheError, self).__init__()

        self._message = message

    def __str__(self) -> str:
        return self._message

    def __reduce__(self):
        return type(self), (self._message,)


class InvalidNameError(DriveError):

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return f'invalid name: {self._name}'


class NodeTreeError(DriveError):

    pass


class NodeConflictedError(NodeTreeError):

    def __init__(self, node: Node) -> None:
        self._node = node

    def __str__(self) -> str:
        return f'node already exists: {self.node.name}'

    @property
    def node(self) -> Node:
        return self._node


class ParentNotFoundError(NodeTreeError):

    def __init__(self, id_: str) -> None:
        self._id = id_

    def __str__(self) -> str:
        return f'parent id not found: {self._id}'


class NodeNotFoundError(NodeTreeError):

    def __init__(self, path_or_id: str) -> None:
        self._path_or_id = path_or_id

    def __str__(self) -> str:
        return f'node not found: {self._path_or_id}'


class RootNodeError(NodeTreeError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message


class TrashedNodeError(NodeTreeError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message


class ParentIsNotFolderError(NodeTreeError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message


class LineageError(NodeTreeError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message


class DownloadError(DriveError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message


class UploadError(DriveError):

    def __init__(self, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return self._message

