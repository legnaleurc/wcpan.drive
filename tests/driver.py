from typing import List, Tuple, AsyncGenerator, Optional
from unittest.mock import Mock

from wcpan.drive.abc import RemoteDriver, ReadableFile, WritableFile, Hasher
from wcpan.drive.types import Node, ChangeDict, PrivateDict
from wcpan.drive.cache import node_from_dict

from .util import create_root


class FakeDriver(RemoteDriver):

    @classmethod
    def get_version_range(cls):
        return (1, 1)

    def __init__(self, config_path, data_path) -> None:
        self._config_path = config_path
        self._data_path = data_path
        self._check_point = '1'
        self._changes = []
        self._cf_mock = Mock()
        self._rn_mock = Mock()
        self._tn_mock = Mock()
        self._d_mock = Mock()
        self._u_mock = Mock()
        self._gh_mock = Mock()

    async def __aenter__(self) -> RemoteDriver:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        pass

    async def get_initial_check_point(self) -> str:
        return '1'

    async def fetch_root_node(self) -> Node:
        data = create_root()
        node = node_from_dict(data)
        return node

    async def fetch_changes(self,
        check_point: str,
    ) -> AsyncGenerator[Tuple[str, List[ChangeDict]], None]:
        yield self._check_point, self._changes

    async def create_folder(self,
        parent_node: Node,
        folder_name: str,
        private: Optional[PrivateDict],
        exist_ok: bool,
    ) -> Node:
        self._cf_mock(parent_node, folder_name, private, exist_ok)
        return await self.fetch_root_node()

    async def rename_node(self,
        node: Node,
        new_parent: Optional[Node],
        new_name: Optional[str],
    ) -> Node:
        self._rn_mock(node, new_parent, new_name)
        return await self.fetch_root_node()

    async def trash_node(self, node: Node) -> None:
        self._tn_mock(node)
        await self.fetch_root_node()

    async def download(self, node: Node) -> ReadableFile:
        self._d_mock(node)
        return

    async def upload(self,
        parent_node: Node,
        file_name: str,
        file_size: Optional[int],
        mime_type: Optional[str],
        private: Optional[PrivateDict],
    ) -> WritableFile:
        self._u_mock(parent_node, file_name, file_size, mime_type, private)
        return

    async def get_hasher(self) -> Hasher:
        self._gh_mock()
        return

    def set_changes(self, check_point: str, changes: List[ChangeDict]) -> None:
        self._check_point = check_point
        self._changes = changes

    @property
    def create_folder_mock(self) -> Mock:
        return self._cf_mock

    @property
    def rename_node_mock(self) -> Mock:
        return self._rn_mock

    @property
    def trash_node_mock(self) -> Mock:
        return self._tn_mock

    @property
    def download_mock(self) -> Mock:
        return self._d_mock

    @property
    def upload_mock(self) -> Mock:
        return self._u_mock

    @property
    def get_hasher_mock(self) -> Mock:
        return self._gh_mock


Driver = FakeDriver
