from collections.abc import AsyncIterator
from typing import Self, override

import numpy

from wcpan.drive.core.exceptions import DriveError
from wcpan.drive.core.lib import dispatch_change
from wcpan.drive.core.types import (
    ChangeAction,
    CreateHasher,
    Hasher,
    Node,
    ReadableFile,
    WritableFile,
)


class InvalidCryptVersion(DriveError):
    pass


class DecryptReadableFile(ReadableFile):
    def __init__(self, stream: ReadableFile) -> None:
        self._stream = stream

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield decrypt(chunk)

    @override
    async def read(self, length: int) -> bytes:
        chunk = await self._stream.read(length)
        return decrypt(chunk)

    @override
    async def seek(self, offset: int) -> int:
        return await self._stream.seek(offset)

    @override
    async def node(self) -> Node:
        return await self._stream.node()


class EncryptWritableFile(WritableFile):
    def __init__(self, stream: WritableFile) -> None:
        self._stream = stream

    @override
    async def tell(self) -> int:
        return await self._stream.tell()

    @override
    async def seek(self, offset: int) -> int:
        return await self._stream.seek(offset)

    @override
    async def write(self, chunk: bytes) -> int:
        crypted = encrypt(chunk)
        return await self._stream.write(crypted)

    @override
    async def flush(self) -> None:
        return await self._stream.flush()

    @override
    async def node(self) -> Node:
        node = await self._stream.node()
        node = decrypt_node(node)
        return node


async def create_hasher(factory: CreateHasher) -> Hasher:
    hasher = await factory()
    return EncryptHasher(hasher)


class EncryptHasher(Hasher):
    def __init__(self, hasher: Hasher) -> None:
        self._hasher = hasher

    @override
    async def update(self, data: bytes) -> None:
        await self._hasher.update(encrypt(data))

    @override
    async def digest(self) -> bytes:
        return await self._hasher.digest()

    @override
    async def hexdigest(self) -> str:
        return await self._hasher.hexdigest()

    @override
    async def copy(self) -> Self:
        hasher = await self._hasher.copy()
        return self.__class__(hasher)


def encrypt(chunk: bytes) -> bytes:
    buffer = numpy.frombuffer(chunk, dtype=numpy.uint8)
    buffer = numpy.bitwise_not(buffer)
    return buffer.tobytes()


def decrypt(chunk: bytes) -> bytes:
    buffer = numpy.frombuffer(chunk, dtype=numpy.uint8)
    buffer = numpy.bitwise_not(buffer)
    return buffer.tobytes()


def encrypt_name(name: str) -> str:
    bname = name.encode("utf-8")
    bname = encrypt(bname)
    return "".join("%02x" % c for c in bname)


def decrypt_name(name: str) -> str:
    hex_list = (name[i : i + 2] for i in range(0, len(name), 2))
    bname = bytes(int(c, 16) for c in hex_list)
    bname = decrypt(bname)
    return bname.decode("utf-8")


def encrypt_node(node: Node) -> Node:
    from dataclasses import replace

    name = encrypt_name(node.name)
    node = replace(node, name=name)
    return node


def decrypt_node(node: Node) -> Node:
    from dataclasses import replace

    name = decrypt_name(node.name)
    node = replace(node, name=name)
    return node


def decode_change(change: ChangeAction) -> ChangeAction:
    return dispatch_change(
        change,
        on_remove=lambda _: (True, _),
        on_update=lambda _: (False, decode_node(_)),
    )


def decode_node(node: Node) -> Node:
    private = node.private
    if not private:
        return node
    if "crypt" not in private:
        return node
    if private["crypt"] != "1":
        raise InvalidCryptVersion()

    node = decrypt_node(node)
    return node
