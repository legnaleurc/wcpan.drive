from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial
from typing import override

from wcpan.drive.client.exceptions import NodeExistsError
from wcpan.drive.client.types import (
    Backend,
    ChangeAction,
    CreateHasher,
    MediaInfo,
    Node,
    PrivateDict,
    ReadableFile,
    WritableFile,
)

from ._lib import (
    DecryptReadableFile,
    EncryptWritableFile,
    InvalidCryptVersion,
    create_hasher,
    decode_change,
    decrypt_node,
    encrypt_name,
    encrypt_node,
)


@asynccontextmanager
async def create_middleware(backend: Backend):
    yield CryptBackend(backend)


class CryptBackend(Backend):
    def __init__(self, fs: Backend):
        self._fs = fs

    @property
    @override
    def api_version(self) -> int:
        return 7

    @override
    async def get_initial_cursor(self) -> str:
        return await self._fs.get_initial_cursor()

    @override
    async def get_root(self) -> Node:
        return await self._fs.get_root()

    @override
    async def purge_trash(self) -> None:
        return await self._fs.purge_trash()

    @override
    async def delete(self, node: Node, *, permanent: bool = False) -> None:
        return await self._fs.delete(node, permanent=permanent)

    @override
    async def restore(self, node: Node) -> Node:
        return await self._fs.restore(node)

    @override
    async def get_changes(
        self,
        cursor: str,
    ) -> AsyncGenerator[tuple[list[ChangeAction], str]]:
        async for changes, next_cursor in self._fs.get_changes(cursor):
            decoded = [decode_change(change) for change in changes]
            yield decoded, next_cursor

    @override
    async def move(
        self,
        node: Node,
        *,
        new_parent: Node | None,
        new_name: str | None,
    ) -> Node:
        private = node.private
        if not private or "crypt" not in private:
            return await self._fs.move(
                node,
                new_parent=new_parent,
                new_name=new_name,
            )
        if private["crypt"] != "1":
            raise InvalidCryptVersion()

        if node.name:
            node = encrypt_node(node)
        if new_name is not None:
            new_name = encrypt_name(new_name)

        try:
            return await self._fs.move(
                node,
                new_parent=new_parent,
                new_name=new_name,
            )
        except NodeExistsError as e:
            raise NodeExistsError(decrypt_node(e.node)) from e

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncGenerator[ReadableFile]:
        private = node.private

        if not private or "crypt" not in private:
            async with self._fs.download_file(node) as fin:
                yield fin
            return

        if private["crypt"] != "1":
            raise InvalidCryptVersion()

        async with self._fs.download_file(node) as fin:
            yield DecryptReadableFile(fin)

    @asynccontextmanager
    @override
    async def upload_file(
        self,
        name: str,
        parent: Node,
        *,
        size: int | None,
        mime_type: str | None,
        media_info: MediaInfo | None,
        private: PrivateDict | None,
    ) -> AsyncGenerator[WritableFile]:
        if private is None:
            private = {}
        if "crypt" not in private:
            private["crypt"] = "1"
        if private["crypt"] != "1":
            raise InvalidCryptVersion()

        name = encrypt_name(name)

        try:
            async with self._fs.upload_file(
                name,
                parent,
                size=size,
                mime_type=mime_type,
                media_info=media_info,
                private=private,
            ) as fout:
                yield EncryptWritableFile(fout)
        except NodeExistsError as e:
            raise NodeExistsError(decrypt_node(e.node)) from e

    @override
    async def create_directory(
        self,
        name: str,
        parent: Node,
        *,
        exist_ok: bool,
        private: PrivateDict | None,
    ) -> Node:
        if private is None:
            private = {}
        if "crypt" not in private:
            private["crypt"] = "1"
        if private["crypt"] != "1":
            raise InvalidCryptVersion()

        name = encrypt_name(name)

        try:
            return await self._fs.create_directory(
                name=name,
                parent=parent,
                exist_ok=exist_ok,
                private=private,
            )
        except NodeExistsError as e:
            raise NodeExistsError(decrypt_node(e.node)) from e

    @override
    async def get_hasher_factory(self, node: Node) -> CreateHasher:
        factory = await self._fs.get_hasher_factory(node)
        return partial(create_hasher, factory)

    @override
    async def is_authenticated(self) -> bool:
        return await self._fs.is_authenticated()

    @override
    async def authenticate(self) -> None:
        return await self._fs.authenticate()
