"""What a backend implements for a scan or comparison pass.

See "Backend framework" in the package ``SPEC.md``; ``scan`` runs the passes.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from wcpan.drive.protocol.types import NodeId, NodeRecord

from ..._types import NodeObservation


class ListingUnavailableError(Exception):
    """The source could not list a folder now, such as while it is unreachable.

    A pass keeps that folder's subtree as mirrored, as for an ``OSError``. Any
    other exception from listing is a bug and fails the pass.
    """


@dataclass(frozen=True, kw_only=True)
class ScanPlan[F]:
    """What one pass covers, decided before any listing."""

    # Mirrored subtrees this pass reconciles.
    scope: set[NodeId]
    roots: list[F]
    # In scope but unusable: kept as mirrored, and the pass is incomplete.
    preserved: list[NodeId]


@dataclass(frozen=True, kw_only=True)
class Listing[F]:
    """One folder as the source reported it."""

    # Every child this listing accounted for.
    seen: list[NodeId]
    # The children to write.
    changed: list[NodeObservation]
    # Folders to visit next.
    subfolders: list[F]
    # Children deliberately not visited; their subtrees stay as mirrored.
    keep: list[NodeId]


class Scanner[F](ABC):
    """The source-specific part of one scan pass or walk.

    *F* is the backend's folder handle. It carries whatever listing the folder
    needs, since a folder can be listed before its own write lands.
    """

    # False: finish traversing one root before starting the next.
    concurrent_roots: bool = True

    @abstractmethod
    async def prepare(self) -> ScanPlan[F]:
        """Decide what this pass covers; a failure stops the pass."""

    @abstractmethod
    def folder_id(self, folder: F) -> NodeId:
        """The node of *folder*."""

    @abstractmethod
    async def list_folder(self, folder: F) -> Listing[F]:
        """List *folder*.

        Raises:
            OSError, ListingUnavailableError: The source cannot list it now;
                its subtree is kept as mirrored. Anything else fails the pass.
        """

    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        """Fetch one removal candidate directly (see ``Locate``)."""

    async def skip(self, folder: F) -> bool:
        """Whether *folder* is known unchanged, keeping its subtree.

        Failures are as for ``list_folder``.
        """
        return False

    async def root_finished(self, root: F, complete: bool) -> None:
        """Called once the pass's writes have landed, such as to save progress."""

    async def pass_finished(self, complete: bool) -> None:
        """Called after every ``root_finished``.

        *complete* is true if every root was fully covered and nothing in scope
        was preserved as unusable.
        """


@dataclass(frozen=True, kw_only=True)
class Contents[F]:
    """One folder as the source reported it, for a ``Comparison``."""

    # Every child the listing reported; the core decides which changed.
    children: list[NodeObservation]
    # Folders to visit next.
    subfolders: list[F]


class Comparison[F](ABC):
    """The source-specific part of a comparison pass.

    Unlike a ``Scanner`` pass, which writes what the backend reports as
    changed, a comparison lists every folder in scope and the core compares
    each child with the mirror. Nothing is skipped and no progress is saved, so
    it repairs what checkpoints would miss.
    """

    @abstractmethod
    async def prepare(self) -> ScanPlan[F]:
        """Decide what this pass covers; a failure stops the pass."""

    @abstractmethod
    def folder_id(self, folder: F) -> NodeId:
        """The node of *folder*."""

    @abstractmethod
    async def list_folder(self, folder: F) -> Contents[F]:
        """List *folder*; failures are as for ``Scanner.list_folder``."""

    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        """Fetch one removal candidate directly (see ``Locate``)."""

    async def differs(
        self, pairs: Sequence[tuple[NodeObservation, NodeRecord]]
    ) -> set[NodeId]:
        """Children whose backend state differs from what the mirror holds.

        *pairs* are one folder's listed children with their mirrored records.
        The core compares the records; this covers what it cannot see, such as
        a locator in a backend table.
        """
        return set()


@dataclass(frozen=True, kw_only=True)
class ComparisonReport:
    """What a comparison pass found; in a dry run, what it would change."""

    # Children the listings reported.
    checked: int
    added: int
    updated: int
    # Subtree roots removed; in a dry run, the unconfirmed candidates.
    removed: int
    # Folders that could not be listed, whose subtrees were kept.
    list_errors: int


class FolderLister[F](Protocol):
    """What ``ScanRunner.walk`` needs; every ``Scanner`` is one."""

    async def list_folder(self, folder: F) -> Listing[F]: ...
