from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from wcpan.drive.core.types import Node, PrivateDict


def aexpect(o: object) -> AsyncMock:
    return cast(AsyncMock, o)


def expect(o: object) -> MagicMock:
    return cast(MagicMock, o)


def create_mock[T](t: type[T]) -> T:
    return cast(T, MagicMock(spec=t))


def create_amock[T](t: type[T]) -> T:
    return cast(T, AsyncMock(spec=t))


def create_node(name: str, private: PrivateDict | None) -> Node:
    return Node(
        id="",
        name=name,
        parent_id="",
        ctime=get_utc_now(),
        mtime=get_utc_now(),
        mime_type="",
        hash="",
        size=0,
        is_trashed=False,
        is_directory=True,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=private,
    )


def get_utc_now():
    return datetime.now(UTC)


async def fake_create_hasher():
    return 42
