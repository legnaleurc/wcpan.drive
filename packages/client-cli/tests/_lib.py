from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

from wcpan.drive.client.types import Node


def create_amock[T](t: type[T]) -> T:
    return cast(T, AsyncMock(spec=t))


def aexpect(o: object) -> AsyncMock:
    return cast(AsyncMock, o)


def make_node(
    *,
    id: str = "test-id",
    parent_id: str | None = "parent-id",
    name: str = "test",
    is_directory: bool = False,
    is_trashed: bool = False,
    size: int = 0,
    mime_type: str = "application/octet-stream",
) -> Node:
    return Node(
        id=id,
        parent_id=parent_id,
        name=name,
        is_directory=is_directory,
        is_trashed=is_trashed,
        created_time=datetime(2024, 1, 1, tzinfo=UTC),
        modified_time=datetime(2024, 1, 2, tzinfo=UTC),
        changed_time=datetime(2024, 1, 2, tzinfo=UTC),
        mime_type=mime_type,
        hash="",
        size=size,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )
