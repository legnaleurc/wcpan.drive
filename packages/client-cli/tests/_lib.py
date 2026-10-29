from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase

from decoy import Decoy
from wcpan.drive.client.types import Drive, Node, NodeQueryBuilder


class DecoyTestCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)


def bind_find_nodes(drive: Drive, *, decoy: Decoy) -> None:
    """Make the mocked drive.find_nodes() return a real builder.

    The builder runs the mocked drive.query_nodes and drive.summarize_nodes.
    """
    decoy.when(drive.find_nodes()).then_do(
        lambda: NodeQueryBuilder(drive.query_nodes, summarize=drive.summarize_nodes)
    )


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
        hash_algorithm="",
        size=size,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )
