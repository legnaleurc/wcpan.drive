from pathlib import Path

from .types import Node


__all__ = (
    "DriveError",
    "InvalidServiceError",
    "NodeExistsError",
    "NodeNotFoundError",
    "NodeIsADirectoryError",
    "UnauthorizedError",
)


class DriveError(Exception):
    pass


class InvalidServiceError(DriveError):
    pass


class NodeExistsError(DriveError):
    def __init__(self, node: Node) -> None:
        super().__init__(f"node already exists: {node.name}")
        self.node = node


class NodeNotFoundError(DriveError):
    def __init__(self, id: str) -> None:
        super().__init__(f"node not found: {id}")
        self.id = id


class NodeIsADirectoryError(DriveError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is a directory")
        self.path = path


class UnauthorizedError(DriveError):
    pass
