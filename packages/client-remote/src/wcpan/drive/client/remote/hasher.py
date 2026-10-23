from typing import Protocol, Self, override

from Crypto.Hash import MD4
from wcpan.drive.client.types import Hasher


class BuiltinHasher(Protocol):
    def update(self, data: bytes) -> None: ...
    def hexdigest(self) -> str: ...
    def digest(self) -> bytes: ...
    def copy(self) -> Self: ...


async def create_hasher() -> Hasher:
    return Md4Hasher(MD4.new())


class Md4Hasher(Hasher):
    def __init__(self, hasher: BuiltinHasher) -> None:
        self._hasher = hasher

    @override
    async def update(self, data: bytes) -> None:
        self._hasher.update(data)

    @override
    async def hexdigest(self) -> str:
        return self._hasher.hexdigest()

    @override
    async def digest(self) -> bytes:
        return self._hasher.digest()

    @override
    async def copy(self) -> Self:
        return self.__class__(self._hasher.copy())
