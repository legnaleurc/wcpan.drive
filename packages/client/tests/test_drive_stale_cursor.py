"""Sync recovers from a stale cursor by rebuilding the source's cache."""

from collections.abc import AsyncIterator, Callable
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from decoy import Decoy, matchers
from wcpan.drive.client._drive import create_drive, create_multi_drive
from wcpan.drive.client.exceptions import NodeNotFoundError, StaleCursorError
from wcpan.drive.client.types import Backend, ChangeAction, SourceConfig

from .test_drive import as_factory, create_backend_mock, make_node


_ROOT = make_node(node_id="root", is_directory=True)
_A = make_node(node_id="a", parent_id="root", name="a")
_B = make_node(node_id="b", parent_id="root", name="b")

type _Pages = list[tuple[list[ChangeAction], str]]


def _feed(pages: dict[str, _Pages | None]) -> Callable[[str], AsyncIterator]:
    """``get_changes`` serving *pages* per cursor; None means the cursor is stale."""

    async def get_changes(cursor: str) -> AsyncIterator[tuple[list[ChangeAction], str]]:
        result = pages[cursor]
        if result is None:
            raise StaleCursorError(cursor)
        for page in result:
            yield page

    return get_changes


async def _backend(decoy: Decoy, pages: dict[str, _Pages | None]) -> Backend:
    backend = create_backend_mock(decoy)
    decoy.when(await backend.is_authenticated()).then_return(True)
    decoy.when(await backend.get_initial_cursor()).then_return("0")
    decoy.when(await backend.get_root()).then_return(_ROOT)
    decoy.when(backend.get_changes(matchers.Anything())).then_do(_feed(pages))
    return backend


async def _collect(it: AsyncIterator[ChangeAction]) -> list[ChangeAction]:
    return [_ async for _ in it]


def _ids(changes: list[ChangeAction]) -> list[tuple[bool, str]]:
    return [(True, c[1]) if c[0] else (False, c[1].id) for c in changes]


class StaleCursorTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.decoy = Decoy()
        self.addCleanup(self.decoy.reset)
        self._tmp = self.enterContext(TemporaryDirectory())

    async def testResetAnnouncesRemovalsThenReplays(self) -> None:
        # given: a cache filled from the old history
        old: _Pages = [([(False, _A), (False, _B)], "e1.2")]
        new: _Pages = [([(False, _A)], "e2.1")]
        backend = await _backend(self.decoy, {"0": old})
        path = f"{self._tmp}/cache.sqlite"
        async with create_drive(backend=as_factory(backend), cache_path=path) as d:
            await _collect(d.sync())
        # the server's history is replaced
        backend = await _backend(self.decoy, {"0": new, "e1.2": None, "e2.1": []})
        async with create_drive(backend=as_factory(backend), cache_path=path) as d:
            # when
            changes = await _collect(d.sync())

            # then: every cached node but the root goes, then the replay
            self.assertCountEqual(_ids(changes[:2]), [(True, "a"), (True, "b")])
            self.assertEqual(_ids(changes[2:]), [(False, "a")])
            self.assertEqual((await d.get_root()).id, "root")
            await d.get_node_by_id("a")
            with self.assertRaises(NodeNotFoundError):
                await d.get_node_by_id("b")
            # and the next sync continues from the new history
            self.assertEqual(await _collect(d.sync()), [])

    async def testRetriesOnlyOnce(self) -> None:
        # given: even the initial cursor is reported stale
        backend = await _backend(self.decoy, {"0": None})
        path = f"{self._tmp}/cache.sqlite"
        async with create_drive(backend=as_factory(backend), cache_path=path) as d:
            # when / then
            with self.assertRaises(StaleCursorError):
                await _collect(d.sync())

    async def testOtherSourcesKeepSyncing(self) -> None:
        # given: two sources synced once
        def sources(stale: Backend, healthy: Backend) -> list[SourceConfig]:
            return [
                SourceConfig(
                    name="stale",
                    backend=as_factory(stale),
                    cache_path=f"{self._tmp}/stale.sqlite",
                ),
                SourceConfig(
                    name="healthy",
                    backend=as_factory(healthy),
                    cache_path=f"{self._tmp}/healthy.sqlite",
                ),
            ]

        first = sources(
            await _backend(self.decoy, {"0": [([(False, _A)], "e1.1")]}),
            await _backend(self.decoy, {"0": [([(False, _B)], "f1.1")]}),
        )
        async with create_multi_drive(first) as d:
            await _collect(d.sync())
        # then one source's history is replaced
        second = sources(
            await _backend(self.decoy, {"0": [([(False, _A)], "e2.1")], "e1.1": None}),
            await _backend(self.decoy, {"f1.1": [([(True, "b")], "f1.2")]}),
        )
        async with create_multi_drive(second) as d:
            # when
            changes = _ids(await _collect(d.sync()))

            # then: the stale source rebuilt itself, the other kept its position
            self.assertCountEqual(
                changes,
                [(True, "stale:a"), (False, "stale:a"), (True, "healthy:b")],
            )
            self.assertLess(
                changes.index((True, "stale:a")), changes.index((False, "stale:a"))
            )
            await d.get_node_by_id("stale:a")
            with self.assertRaises(NodeNotFoundError):
                await d.get_node_by_id("healthy:b")
