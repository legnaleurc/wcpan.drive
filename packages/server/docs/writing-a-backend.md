# Writing a storage backend

A backend connects `wcpan-drive-server` to one kind of storage. The core runs the
sync flow: traversal, read ordering, media probing, the write queue, startup, and
readiness. A backend supplies what is specific to its source.

Import the core only through its public modules:

| Module | Use |
|---|---|
| `wcpan.drive.server.types` | Classes to extend and types you receive |
| `wcpan.drive.server.lib` | Helpers such as `parallel_bfs` and `SpooledFiles` |
| `wcpan.drive.server.exceptions` | Errors the core understands |
| `wcpan.drive.server.testing` | Running your backend in tests only |

The design and its reasons are in [`SPEC.md`](../SPEC.md), under "Backend framework".

## Packaging

A backend is an async context manager factory. Register it so `init` can find it:

```toml
[project.entry-points."wcpan.drive.server.backend"]
memory = "acme_drive.memory:create_backend"
```

The package can use any namespace. A server config selects it by import path:

```yaml
backends:
  scratch:
    type: acme_drive.memory.create_backend
mounts:
  docs:
    backend: scratch
    source: docs
```

The factory's keyword-only parameters are its settings. Annotate them with
`Annotated[type, "help text"]`, and `init` turns them into a commented config.
`mounts[...].source` is yours to interpret.

## What you implement

| Piece | Purpose |
|---|---|
| Factory | Binds the schema, connects, and yields the backend |
| `BackendSchema` | Your tables, kept in the core's transactions |
| `Scanner` | How to list the source, for the startup scan and rescans |
| `ChangeHandler` | Optional: turns notifications into changes |
| `Comparison` | Optional: a full re-check of a subtree, for a repair command |
| `NodeOperations` | Create, rename, move, delete, download |
| `Uploader` and `FileOpener` | Uploads; `SpooledFiles` covers a source that takes whole files |
| `startup_scan`, `change_handler`, `run` | What the core starts, in that order |

## Complete example

This backend stores files in memory. The test suite runs this exact code.

```python
import hashlib
import sqlite3
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from wcpan.drive.protocol.types import MediaInfo, NodeId, NodeRecord
from wcpan.drive.server.exceptions import (
    OperationConflictError,
    OperationNotFoundError,
    UploadConflictError,
)
from wcpan.drive.server.lib import SpooledFiles, create_spool_area, writing
from wcpan.drive.server.types import (
    BackendCommand,
    BackendContext,
    BackendLocator,
    BackendNamespace,
    ChangeHandler,
    CoreServices,
    Listing,
    NodeObservation,
    Scanner,
    ScanPlan,
)


@dataclass
class Item:
    key: str  # stable across renames and moves
    parent: str
    name: str
    is_directory: bool = False
    data: bytes = b""


class Source:
    """Stands in for the client of a real storage service."""

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}

    def add(self, parent: str, name: str, *, data: bytes = b"", folder=False) -> Item:
        item = Item(uuid.uuid4().hex, parent, name, folder, data)
        self.items[item.key] = item
        return item

    def children(self, parent: str) -> list[Item]:
        return [i for i in self.items.values() if i.parent == parent]

    def child(self, parent: str, name: str) -> Item | None:
        return next((i for i in self.children(parent) if i.name == name), None)

    def remove(self, key: str) -> None:
        for child in self.children(key):
            self.remove(child.key)
        del self.items[key]


SOURCES: dict[str, Source] = {}  # by instance name


class MemorySchema:
    """This backend keeps no tables of its own."""

    name = "memory"
    version = 1

    def ensure_schema(self, con: sqlite3.Connection) -> None:
        pass

    def bind_locator(
        self,
        con: sqlite3.Connection,
        ns: BackendNamespace,
        node_id: NodeId,
        locator: BackendLocator,
    ) -> None:
        pass

    def forget_nodes(
        self, con: sqlite3.Connection, ns: BackendNamespace, node_ids: Sequence[str]
    ) -> None:
        pass

    def forget_orphans(self, con: sqlite3.Connection, ns: BackendNamespace) -> None:
        pass


SCHEMA = MemorySchema()


class Mapping:
    """Translates between source items and mirror nodes."""

    def __init__(self, context: BackendContext, core: CoreServices) -> None:
        self.mounts = context.mounts
        self.ids = core.ids

    async def key_of(self, node_id: NodeId) -> str:
        mount = self.mounts.by_id(node_id)
        return mount.source if mount else await self.ids.native_of(node_id)

    async def parent_id(self, key: str) -> NodeId:
        for mount in self.mounts.values():
            if mount.source == key:
                return mount.id
        (node_id,) = await self.ids.public([key])
        return node_id

    async def observe(self, item: Item) -> NodeObservation:
        (node_id,) = await self.ids.public([item.key])
        data = item.data
        record = NodeRecord(
            id=node_id,
            parent_id=await self.parent_id(item.parent),
            name=item.name,
            is_directory=item.is_directory,
            created_time=0,
            modified_time=0,
            changed_time=0,
            mime_type=(
                "application/x-directory"
                if item.is_directory
                else "application/octet-stream"
            ),
            hash="" if item.is_directory else hashlib.md5(data).hexdigest(),
            hash_algorithm="" if item.is_directory else "md5",
            size=len(data),
            is_image=False,
            is_video=False,
            width=0,
            height=0,
        )
        return NodeObservation(record=record, locator=None, source_key=item.key)


@dataclass(frozen=True)
class Folder:
    node_id: NodeId
    key: str


class FullScan(Scanner[Folder]):
    """One pass over every mount; create a new instance for each pass."""

    def __init__(self, source: Source, mapping: Mapping) -> None:
        self._source = source
        self._mapping = mapping

    async def prepare(self) -> ScanPlan[Folder]:
        mounts = list(self._mapping.mounts.values())
        return ScanPlan(
            scope={m.id for m in mounts},
            roots=[Folder(m.id, m.source) for m in mounts],
            preserved=[],
        )

    def folder_id(self, folder: Folder) -> NodeId:
        return folder.node_id

    async def list_folder(self, folder: Folder) -> Listing[Folder]:
        observed = [
            (item, await self._mapping.observe(item))
            for item in self._source.children(folder.key)
        ]
        return Listing(
            seen=[o.record.id for _, o in observed],
            changed=[o for _, o in observed],
            subfolders=[
                Folder(o.record.id, item.key)
                for item, o in observed
                if item.is_directory
            ],
            keep=[],
        )

    async def locate(self, node_id: NodeId) -> NodeObservation | None:
        item = self._source.items.get(await self._mapping.key_of(node_id))
        return await self._mapping.observe(item) if item else None


class FollowChanges(ChangeHandler[str]):
    """Runs one event at a time, after the startup scan."""

    continue_after_failure = True  # the next change or rescan repairs the mirror

    def __init__(self, core: CoreServices, source: Source, mapping: Mapping) -> None:
        self._core = core
        self._source = source
        self._mapping = mapping

    async def handle(self, event: str) -> None:
        await self._core.scans.run(FullScan(self._source, self._mapping))


class Operations:
    def __init__(self, core: CoreServices, source: Source, mapping: Mapping) -> None:
        self._core = core
        self._source = source
        self._mapping = mapping

    async def create_folder(self, parent_id: NodeId, name: str) -> NodeRecord:
        async with self._core.observe() as changes:
            parent = await self._mapping.key_of(parent_id)
            if self._source.child(parent, name) is not None:
                raise OperationConflictError(f"{name!r} already exists")
            item = self._source.add(parent, name, folder=True)
            return await changes.upserted(await self._mapping.observe(item))

    async def update(
        self, record: NodeRecord, *, name: str | None, parent_id: NodeId | None
    ) -> NodeRecord:
        async with self._core.observe() as changes:
            key = await self._mapping.key_of(record.id)
            item = self._source.items.get(key)
            if item is None:
                raise OperationNotFoundError(f"{record.name!r} is gone")
            if parent_id is not None:
                item.parent = await self._mapping.key_of(parent_id)
            if name is not None:
                item.name = name
            return await changes.upserted(await self._mapping.observe(item))

    async def delete(self, record: NodeRecord) -> None:
        async with self._core.observe() as changes:
            key = await self._mapping.key_of(record.id)
            if key in self._source.items:
                self._source.remove(key)
            await changes.removed(record.id)

    @asynccontextmanager
    async def download(
        self, record: NodeRecord, *, offset: int, length: int
    ) -> AsyncGenerator[AsyncIterable[bytes]]:
        key = await self._mapping.key_of(record.id)
        data = self._source.items[key].data[offset : offset + length]

        async def chunks() -> AsyncGenerator[bytes]:
            yield data

        yield chunks()


class Uploads:
    def __init__(self, core: CoreServices, source: Source, mapping: Mapping) -> None:
        self._core = core
        self._source = source
        self._mapping = mapping

    async def find_conflict(self, parent_id: NodeId, name: str) -> NodeRecord | None:
        parent = await self._mapping.key_of(parent_id)
        item = self._source.child(parent, name)
        return (await self._mapping.observe(item)).record if item else None

    async def upload(
        self,
        parent_id: NodeId,
        name: str,
        content: AsyncIterable[bytes],
        *,
        mime_type: str | None,
        media_info: MediaInfo | None,
    ) -> NodeRecord:
        data = b"".join([chunk async for chunk in content])
        async with self._core.observe() as changes:
            existing = await self.find_conflict(parent_id, name)
            if existing is not None:
                raise UploadConflictError(existing)
            parent = await self._mapping.key_of(parent_id)
            item = self._source.add(parent, name, data=data)
            observation = await self._mapping.observe(item)
            return await changes.upserted(observation, strict=True)


class MemoryBackend:
    def __init__(self, context: BackendContext) -> None:
        self._context = context
        self.source = SOURCES.setdefault(context.name, Source())

    def serve(self, area) -> None:
        core = writing(self._context.core)
        self._core = core
        self._mapping = Mapping(self._context, core)
        self.operations = Operations(core, self.source, self._mapping)
        self.uploader = Uploads(core, self.source, self._mapping)
        self.files = SpooledFiles(uploader=self.uploader, area=area, media=core.media)

    def notify(self, what: str) -> None:
        """Called when the source reports a change; never blocks."""
        self._core.hints.publish(what)

    async def startup_scan(self) -> None:
        await self._core.scans.run(FullScan(self.source, self._mapping))

    def change_handler(self) -> FollowChanges:
        return FollowChanges(self._core, self.source, self._mapping)

    async def run(self) -> None:
        """Nothing to do besides handling changes, which the core runs."""

    async def run_command(self, command: BackendCommand) -> None:
        raise NotImplementedError


@asynccontextmanager
async def create_backend(context: BackendContext) -> AsyncGenerator[MemoryBackend]:
    await context.core.bind_schema(SCHEMA)
    backend = MemoryBackend(context)
    if context.offline:  # a maintenance command: connect to nothing yet
        yield backend
        return
    with create_spool_area(tmp_dir=None) as area:
        backend.serve(area)
        yield backend
```

## Scanning

`core.scans.run(scanner)` runs one pass, after any pass before it. Create a new
`Scanner` for each pass, so per-pass state can live on it. The core calls your hooks:

- `prepare` decides the scope and roots. Put a root you cannot use safely, such as
  an unmounted disk, in `preserved`, so its mirror is kept.
- `list_folder` returns every child in `seen` and those to write in `changed`. When the
  source cannot list a folder now, raise `OSError` or `ListingUnavailableError`: the
  subtree is kept and its root is incomplete. Any other exception fails the pass, so
  translate only your source's own failures and let bugs propagate.
- `skip` may return true for a folder known to be unchanged. `keep` lists children
  you deliberately do not visit.
- `locate` confirms each removal candidate. Raise `LocateUndecidedError` if the
  source cannot tell, and the node is kept.
- `root_finished` and `pass_finished` run after the pass's writes have landed. Save
  progress here, or after the pass returns, never earlier.

A folder may be listed before its own write lands, so your folder handle must carry
whatever listing it needs.

## Writing changes

Every change to the mirror comes from a `core.observe()` block:

```python
async with core.observe() as changes:
    info = await client.rename(item, name)
    record = await changes.upserted(to_observation(info))
```

- Enter the block **before the first source read** your write depends on. Its read
  number decides which of two racing observations wins.
- Change the source first, then the mirror. No transaction is open during source
  calls. If the process stops between them, the next scan repairs the mirror.
- A failed mirror write stops the whole server. Do not try to recover from it.
- `upserted` probes media in the caller. `strict=True` raises on a failed probe.
- `await changes.landed()` waits for writes queued so far. Use it when your next
  read depends on them. The server already waits before answering an API call.
- Use `core.observe(exclusive=True)` if a scan could otherwise mint a second ID for
  an object you are creating. It holds off scans until your writes land. Never start
  a scan inside the block.

Raise `Operation*Error` and `Upload*Error` from `wcpan.drive.server.exceptions`, so
the server can answer with the right status.

## Following changes

Publish raw events and do the work in `ChangeHandler.handle`:

```python
core.hints.publish(event)    # from a webhook route or watcher; never blocks
```

- Do not read anything before publishing. `handle` runs one event at a time, after
  the startup scan and after every earlier event has returned.
- An earlier event's writes may still be queued when the next event starts. If a later
  event depends on them, such as a child that needs its new parent, call
  `await changes.landed()` before `handle` returns.
- `core.hints.later(key, delay, work)` debounces. A newer call with the same key
  replaces a pending timer but never cancels running work.
- A failed event stops the server unless `continue_after_failure = True`. Keep the
  default if nothing else would repair the mirror.
- `context.routes.add("POST", "webhook", handler)` serves
  `/api/v1/backends/<instance>/webhook` and returns that path to advertise. Handlers
  are `aiohttp` handlers, and these routes work during the startup scan.

Merge noisy sources before publishing, then `await core.hints.join()` so changes that
arrive during a pass collapse into the next batch.

## Your own tables

Implement `BackendSchema` for state such as locators. Hooks run synchronously on the
core's connection, inside its transaction, and must only run SQL. Every table needs
an `instance` column filtered by `BackendNamespace.key`.

- A `locator` on a `NodeObservation` is stored with its node by `bind_locator`.
- Progress such as a checkpoint uses `await core.enqueue_write(fn, ...)`, which runs
  `fn(con, ...)` as one transaction in order with other writes.
- Read with `await core.mirror.read(fn, ...)`.

## Media

If your source holds images or videos, register how to read them while connected:

```python
with core.media.register(context.name, open_source):
    async with core.enrichment.retrying(locate_by_source_key):
        yield backend
```

Set `source_key` on observations, so a failed probe can be recorded and retried. Files
whose probe fails are withheld until a retry or later scan succeeds.

## Maintenance commands

Decorate the factory with `backend_commands(parser)` to add commands. The parser
returns a `BackendCommand` declaring the access it needs, and `run_command` runs it
with the server stopped. Read-only commands get `ReadCoreServices`. To repair a subtree
without trusting checkpoints, implement `Comparison`: like a `Scanner`, but `list_folder`
returns every child in `Contents` and the core decides what changed. Override `differs`
if some of your state, such as a locator in your own table, can change while the record
does not. Run it with `core.compare(comparison, dry_run=...)`, which returns a
`ComparisonReport`; a read-only command can only run a dry run.

## Testing

`wcpan.drive.server.testing` runs your backend against a real core and a temporary
database, without HTTP:

```python
from wcpan.drive.server.testing import BackendLifecycle, backend_harness, enter_backend

async with backend_harness() as server:
    instance = await server.add_instance("scratch", SCHEMA, {"docs": "docs"})
    async with enter_backend(create_backend, instance.context, {}) as backend:
        await BackendLifecycle(backend, instance.core).until_ready()
```

`until_ready` waits for handled hints, so run `lifecycle.run()` as a task when your
backend publishes any. A failed write stops a real server, so in the harness it
interrupts the block and is raised on exit. `server.probes` fakes media probing, and `server.hold_writes()`
lets a test force an ordering without sleeping.
