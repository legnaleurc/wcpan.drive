import hashlib
from functools import partial
from typing import Protocol, Self, override

from Crypto.Hash import MD4
from wcpan.drive.client.exceptions import UnsupportedHashAlgorithmError
from wcpan.drive.client.types import CreateHasher, Hasher, Node


class BuiltinHasher(Protocol):
    def update(self, data: bytes, /) -> None: ...
    def hexdigest(self) -> str: ...
    def digest(self) -> bytes: ...
    def copy(self) -> Self: ...


async def create_md4_hasher() -> Hasher:
    return WrappedHasher(MD4.new())


async def create_hashlib_hasher(name: str) -> Hasher:
    return WrappedHasher(hashlib.new(name))


# Algorithms reported by server backends; each value is a whole-file
# lowercase hex digest. Factories must stay picklable for the CLI process pool.
_FACTORIES: dict[str, CreateHasher] = {
    # Synology Drive; not reliably available from hashlib with OpenSSL 3.
    "md4": create_md4_hasher,
    "md5": partial(create_hashlib_hasher, "md5"),
    "sha1": partial(create_hashlib_hasher, "sha1"),
    "sha256": partial(create_hashlib_hasher, "sha256"),
}


def get_hasher_factory(node: Node) -> CreateHasher:
    """Select the hasher matching the algorithm reported by the server."""
    if node.is_directory:
        raise UnsupportedHashAlgorithmError(node)
    factory = _FACTORIES.get(node.hash_algorithm)
    if factory is None:
        raise UnsupportedHashAlgorithmError(node)
    return factory


class WrappedHasher(Hasher):
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
