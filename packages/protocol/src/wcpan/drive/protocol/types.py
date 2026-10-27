from dataclasses import dataclass
from typing import NewType


NodeId = NewType("NodeId", str)


@dataclass(frozen=True, kw_only=True)
class NodeRecord:
    """Wire-format node.

    ``id`` / ``parent_id`` are the public mirror IDs.
    ``hash`` is the content digest and ``hash_algorithm`` names how it was
    computed; the backend defines the identifier and clients interpret it.
    """

    id: NodeId
    parent_id: NodeId | None
    name: str
    is_directory: bool
    created_time: int
    modified_time: int
    changed_time: int
    mime_type: str
    hash: str
    hash_algorithm: str
    size: int
    is_image: bool
    is_video: bool
    width: int
    height: int
    ms_duration: int


@dataclass(frozen=True, kw_only=True)
class MediaInfo:
    is_image: bool = False
    is_video: bool = False
    width: int = 0
    height: int = 0
    ms_duration: int = 0
