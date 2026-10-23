"""Order mirror writes by when the backend read that produced them began.

A notification is delivered after the change it reports, so the read it triggers
starts after that change; an overlapping read that started earlier may be stale.
Each write therefore carries the sequence number of its read, and is admitted only
if no observation from a later-started read was already applied to the node.
"""

from wcpan.drive.protocol.types import MirrorStableId

from ..types import ReadSeq


_MIN_PRUNE_SIZE = 1024


class ObservationOrder:
    """In-process bookkeeping for read-start ordering.

    A read stays outstanding while it is in progress and while any write derived
    from it is pending (``hold``/``release``). Applied sequence numbers older than
    every outstanding read can no longer reject a write, so they are pruned.

    Callers must run ``admits``/``record`` from the serialized write worker.
    """

    def __init__(self) -> None:
        self._next = 0
        self._outstanding: dict[ReadSeq, int] = {}
        self._applied: dict[MirrorStableId, ReadSeq] = {}
        self._prune_at = _MIN_PRUNE_SIZE

    def begin(self) -> ReadSeq:
        seq = ReadSeq(self._next)
        self._next += 1
        self._outstanding[seq] = 1
        return seq

    def hold(self, seq: ReadSeq) -> None:
        self._outstanding[seq] += 1

    def release(self, seq: ReadSeq) -> None:
        count = self._outstanding[seq] - 1
        if count:
            self._outstanding[seq] = count
            return
        del self._outstanding[seq]
        self._maybe_prune()

    def admits(self, node_id: MirrorStableId, seq: ReadSeq) -> bool:
        """True unless *node_id* already holds an observation from a later read."""
        return self._applied.get(node_id, seq) <= seq

    def record(self, node_id: MirrorStableId, seq: ReadSeq) -> None:
        """Note that an observation from *seq* was applied to *node_id*."""
        if self.admits(node_id, seq):
            self._applied[node_id] = seq

    def touched_after(self, seq: ReadSeq) -> set[MirrorStableId]:
        """Nodes written or removed by reads that started after *seq*.

        Complete while *seq* is outstanding, since pruning keeps those entries.
        """
        return {node_id for node_id, s in self._applied.items() if s > seq}

    def _maybe_prune(self) -> None:
        if len(self._applied) < self._prune_at:
            return
        floor = min(self._outstanding, default=self._next)
        self._applied = {k: v for k, v in self._applied.items() if v >= floor}
        self._prune_at = max(_MIN_PRUNE_SIZE, 2 * len(self._applied))
