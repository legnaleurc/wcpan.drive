from datetime import UTC, datetime
from typing import Self
from unittest import IsolatedAsyncioTestCase

from decoy import Decoy
from wcpan.drive.client.types import Hasher, Node, PrivateDict


class DecoyTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)


def create_node(name: str, private: PrivateDict | None) -> Node:
    return Node(
        id="",
        name=name,
        parent_id="",
        created_time=get_utc_now(),
        modified_time=get_utc_now(),
        changed_time=get_utc_now(),
        mime_type="",
        hash="",
        hash_algorithm="",
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


class FakeHasher(Hasher):
    async def update(self, data: bytes) -> None:
        pass

    async def digest(self) -> bytes:
        return b""

    async def hexdigest(self) -> str:
        return ""

    async def copy(self) -> Self:
        return self


async def fake_create_hasher() -> Hasher:
    return FakeHasher()
