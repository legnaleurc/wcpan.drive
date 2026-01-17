from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import override

from wcpan.drive.core.exceptions import NodeExistsError
from wcpan.drive.core.types import (
    ChangeAction,
    CreateHasher,
    FileService,
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
async def create_service(file_service: FileService):
    yield CryptFileService(file_service)


class CryptFileService(FileService):
    def __init__(self, fs: FileService):
        self._fs = fs

    @property
    @override
    def api_version(self) -> int:
        return 4

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
    async def delete(self, node: Node) -> None:
        return await self._fs.delete(node)

    @override
    async def get_changes(
        self,
        cursor: str,
    ) -> AsyncIterator[tuple[list[ChangeAction], str]]:
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
        trashed: bool | None,
    ) -> Node:
        private = node.private
        if not private or "crypt" not in private:
            return await self._fs.move(
                node,
                new_parent=new_parent,
                new_name=new_name,
                trashed=trashed,
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
                trashed=trashed,
            )
        except NodeExistsError as e:
            raise NodeExistsError(decrypt_node(e.node)) from e

    @asynccontextmanager
    @override
    async def download_file(self, node: Node) -> AsyncIterator[ReadableFile]:
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
    ) -> AsyncIterator[WritableFile]:
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
    async def get_hasher_factory(self) -> CreateHasher:
        factory = await self._fs.get_hasher_factory()
        return partial(create_hasher, factory)

    @override
    async def is_authorized(self) -> bool:
        return await self._fs.is_authorized()

    @override
    async def get_oauth_url(self) -> str:
        return await self._fs.get_oauth_url()

    @override
    async def set_oauth_token(self, token: str) -> None:
        return await self._fs.set_oauth_token(token)
