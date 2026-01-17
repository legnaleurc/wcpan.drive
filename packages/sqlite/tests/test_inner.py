from unittest import TestCase

from tests._lib import (
    create_sandbox,
    random_dir,
    random_file,
    random_image,
    random_video,
)
from wcpan.drive.sqlite._inner import inner_get_node_by_id, inner_insert_node
from wcpan.drive.sqlite._lib import read_only, read_write


class SerializationTest(TestCase):
    def setUp(self) -> None:
        self._dsn, self._root = self.enterContext(create_sandbox())

    def testDirectory(self):
        expected = random_dir(self._root.id)

        with read_write(self._dsn) as query:
            inner_insert_node(query, expected)

        with read_only(self._dsn) as query:
            rv = inner_get_node_by_id(query, expected.id)

        self.assertEqual(rv, expected)

    def testFile(self):
        expected = random_file(self._root.id)

        with read_write(self._dsn) as query:
            inner_insert_node(query, expected)

        with read_only(self._dsn) as query:
            rv = inner_get_node_by_id(query, expected.id)

        self.assertEqual(rv, expected)

    def testImage(self):
        expected = random_image(self._root.id)

        with read_write(self._dsn) as query:
            inner_insert_node(query, expected)

        with read_only(self._dsn) as query:
            rv = inner_get_node_by_id(query, expected.id)

        self.assertEqual(rv, expected)

    def testVideo(self):
        expected = random_video(self._root.id)

        with read_write(self._dsn) as query:
            inner_insert_node(query, expected)

        with read_only(self._dsn) as query:
            rv = inner_get_node_by_id(query, expected.id)

        self.assertEqual(rv, expected)
