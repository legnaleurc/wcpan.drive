from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from wcpan.drive.client.types import NodeQuery, NodeQueryBuilder, NodeSummary

from .test_drive import make_node


class NodeQueryTestCase(TestCase):
    def testDefaultExcludesTrashed(self) -> None:
        # when / then
        self.assertIs(NodeQuery().is_trashed, False)

    def testInvalidPatternRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(name_pattern="(")

    def testEmptyMimeTypePrefixRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(mime_type_prefix="")

    def testNegativeSizeRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(size_min=-1)

    def testEmptySizeRangeRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(size_min=2, size_max=1)

    def testSingleSizeIsAllowed(self) -> None:
        # when / then
        NodeQuery(size_min=0, size_max=0)

    def testNaiveTimeRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(modified_after=datetime(2026, 1, 1))

    def testEmptyTimeRangeRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(
                created_after=datetime(2026, 1, 2, tzinfo=UTC),
                created_before=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def testDirectoryWithFileConditionRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            NodeQuery(is_directory=True, size_max=0)
        with self.assertRaises(ValueError):
            NodeQuery(is_directory=True, mime_type_prefix="video/")

    def testNonPositiveLimitRaises(self) -> None:
        # given
        for limit in (0, -1):
            # when / then
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                NodeQuery(limit=limit)


class NodeQueryBuilderTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._run = AsyncMock(return_value=[])
        self._summarize = AsyncMock(return_value=NodeSummary(count=2, total_size=42))
        self._builder = NodeQueryBuilder(self._run, summarize=self._summarize)

    def testEmptyBuilder(self) -> None:
        # when / then
        self.assertEqual(self._builder.to_query(), NodeQuery())

    def testAllConditions(self) -> None:
        # given
        after = datetime(2026, 1, 1, tzinfo=UTC)
        before = datetime(2026, 2, 1, tzinfo=UTC)
        # when
        query = (
            self._builder.descendant_of(make_node(node_id="a"))
            .files_only()
            .trashed_only()
            .name_matches(r"\.mkv$")
            .mime_type("video/")
            .size(min=1, max=2)
            .created(after=after, before=before)
            .modified(after=after, before=before)
            .limit(3)
            .to_query()
        )
        # then
        self.assertEqual(
            query,
            NodeQuery(
                ancestor_id="a",
                is_directory=False,
                is_trashed=True,
                name_pattern=r"\.mkv$",
                mime_type_prefix="video/",
                size_min=1,
                size_max=2,
                created_after=after,
                created_before=before,
                modified_after=after,
                modified_before=before,
                limit=3,
            ),
        )

    def testIncludeTrashed(self) -> None:
        # when
        query = self._builder.include_trashed().to_query()
        # then
        self.assertIsNone(query.is_trashed)

    def testRepeatedCallReplacesWholeRange(self) -> None:
        # when
        query = self._builder.size(min=10).size(max=20).to_query()
        # then
        self.assertIsNone(query.size_min)
        self.assertEqual(query.size_max, 20)

    def testDirectoriesOnlyWithSizeRaises(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            self._builder.directories_only().size(max=0).to_query()

    async def testAllRunsQuery(self) -> None:
        # given
        node = make_node(node_id="a")
        self._run.return_value = [node]

        # when
        rv = await self._builder.size(max=0).all()

        # then
        self.assertEqual(rv, [node])
        self._run.assert_awaited_once_with(NodeQuery(size_max=0))

    async def testAllValidatesBeforeRunning(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            await self._builder.limit(0).all()
        # then
        self._run.assert_not_awaited()

    async def testSummarizeRunsWithoutLoadingNodes(self) -> None:
        # when
        result = await self._builder.files_only().summarize()
        # then
        self.assertEqual(result, NodeSummary(count=2, total_size=42))
        self._summarize.assert_awaited_once_with(NodeQuery(is_directory=False))
        self._run.assert_not_awaited()

    async def testSummarizeRejectsLimit(self) -> None:
        # when / then
        with self.assertRaises(ValueError):
            await self._builder.limit(1).summarize()
        # then
        self._summarize.assert_not_awaited()
