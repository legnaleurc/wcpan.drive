"""Read-start ordering of mirror writes."""

from unittest import TestCase

from wcpan.drive.protocol.types import NodeId
from wcpan.drive.server.services import ordering
from wcpan.drive.server.services.ordering import ObservationOrder


_X = NodeId("x")


class TestObservationOrder(TestCase):
    def test_later_read_wins_regardless_of_apply_order(self) -> None:
        order = ObservationOrder()
        older = order.begin()
        newer = order.begin()

        order.record(_X, newer)

        self.assertFalse(order.admits(_X, older))
        self.assertTrue(order.admits(_X, newer))

    def test_older_read_applied_first_does_not_block_newer(self) -> None:
        order = ObservationOrder()
        older = order.begin()
        newer = order.begin()

        order.record(_X, older)

        self.assertTrue(order.admits(_X, newer))

    def test_touched_after_reports_only_later_reads(self) -> None:
        order = ObservationOrder()
        scan = order.begin()
        before = NodeId("before")
        order.record(before, scan)
        later = order.begin()
        order.record(_X, later)

        self.assertEqual(order.touched_after(scan), {_X})

    def test_pruning_keeps_entries_that_can_reject_outstanding_reads(self) -> None:
        order = ObservationOrder()
        stale = order.begin()
        newer = order.begin()
        order.record(_X, newer)
        order.release(newer)
        # Force pruning by filling past the threshold with finished reads.
        for i in range(ordering._MIN_PRUNE_SIZE + 1):
            seq = order.begin()
            order.record(NodeId(f"n{i}"), seq)
            order.release(seq)

        # "stale" is still outstanding, so the newer entry must survive pruning.
        self.assertFalse(order.admits(_X, stale))

    def test_pruning_drops_entries_older_than_every_outstanding_read(self) -> None:
        order = ObservationOrder()
        first = order.begin()
        order.record(_X, first)
        order.release(first)
        for i in range(ordering._MIN_PRUNE_SIZE + 1):
            seq = order.begin()
            order.record(NodeId(f"n{i}"), seq)
            order.release(seq)

        self.assertNotIn(_X, order.touched_after(-1))  # type: ignore[arg-type]

    def test_held_read_stays_outstanding_after_release(self) -> None:
        order = ObservationOrder()
        stale = order.begin()
        order.hold(stale)
        order.release(stale)  # the read itself finished; a write is still pending
        newer = order.begin()
        order.record(_X, newer)
        order.release(newer)
        for i in range(ordering._MIN_PRUNE_SIZE + 1):
            seq = order.begin()
            order.record(NodeId(f"n{i}"), seq)
            order.release(seq)

        self.assertFalse(order.admits(_X, stale))
