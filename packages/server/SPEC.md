# wcpan-drive-server design

Invariants of the generic server and the reasons for them. Tests are the source
of truth for exact behavior; this file says what must stay true and why, and
leaves implementation details to the code. User-facing usage and operator
steps are in `README.md`.

## Storage

### One database

The mirror (`nodes`, `changes`), the ID map, the backend instance registry, media
failure records, server metadata, and the tables of every backend live in one
SQLite file.

No operation writes to two backends: the router refuses moves between backends,
and uploads, creates, and deletes go to one backend. Isolating backends therefore
needs an ID map and instance-scoped rows, not separate files. One file keeps one
`change_id` sequence (one total order and a simple cursor), and a node, its change
row, and its backend locator commit in one transaction.

The core schema version is SQLite's `user_version`. There is no in-place
migration: a database at another version is refused with a message to move it
aside, and the server then builds a new one by initial discovery. Backend tables
have their own versions (see Backend tables).

Connection settings and transaction semantics come from `wcpan-drive-sqlite`
(`packages/sqlite/SPEC.md`).

### Write queue

While the server runs, every database write runs inside a job on the single write
queue (`WriteQueue`, consumed by `write_worker`). The worker does not wrap a job in
a transaction: each `MirrorStore.write` call is one transaction.

- Queue order is the order of state transitions. Related writes must happen in
  one `MirrorStore.write` call; separate calls are separate commits, even within
  one job.
- A job reads current state, checks its conditions, and writes in that one
  transaction. For example, `apply_observations` skips an observation whose
  parent is gone, so an observation made before its parent was removed cannot
  create an orphan.
- Network calls and filesystem reads happen before the job. Their results can be
  stale by the time the job runs, so jobs validate them when applying. The queue
  serializes database state, not the external source.
- Jobs only run SQL. Media probes, hashing, transfers, and source calls finish
  before a job is queued, and backend hooks run synchronously on the core's
  connection. API mutations wait for the queue (see Mutation visibility), so a
  slow job would delay every client's writes.
- Anything that waits on the queue (`run_queued`, `NodeIdMap.public`) deadlocks
  when called from inside a job.
- A failed write stops the worker, and with it the server. A write failure is
  treated as an unstable system; the next start recovers from the database.
- Reads use read-only snapshots and do not go through the queue.
- Offline commands (`drop`, `rebuild`, `gc`, `squash`) run while the server is
  stopped and write directly.

The metadata (enrichment) queue is separate and produces write jobs. All backends
share both queues, `ObservationOrder`, and the enricher, so one backend's
enrichment backlog can delay another's writes.

### Backend tables

A backend persists its state through `BackendSchema` hooks, which run on the
core's connection inside the core's transaction. Hooks must not commit, roll back,
or open connections. Backend persistence is SQLite-specific on purpose, so there
is no storage abstraction.

- Table names are fixed, and every instance of a backend shares them. Each table
  has an `instance` column holding the instance's integer registry key
  (`BackendNamespace.key`), and every query filters by it. Backend rows are keyed
  by public node ID, which is unique across instances; lookups by native locator
  are keyed by `(instance, ...)`.
- Every query lives in the backend's store, which holds the key. A query that
  misses the instance filter would leak rows between instances; keeping queries
  in the store contains that risk.
- The core finds backend tables without loading backends: every table outside
  `CORE_TABLES` that has an `instance` column. `drop` and `rebuild` delete an
  instance's rows through that column.
- Versions are per backend (`BackendSchema.name` in `backend_schemas`), since its
  instances share the tables. A mismatch is refused at startup and needs a new
  database; `rebuild` cannot fix it.
- An instance is bound to one `BackendSchema.name` when it first registers. A
  later change of backend is refused until the instance is dropped.
- A locator commits with its node's observation. Locator-only changes emit no
  change row.
- Foreign keys are off. Removal paths call `forget_nodes`. Rows of nodes removed
  while the backend was not loaded (by offline `gc`) are dropped by
  `forget_orphans` when the instance next registers.

Shared tables keep schema versions consistent across instances and avoid
dynamic table names in queries and maintenance.

## Identity

### Public node IDs

Public node IDs are opaque random tokens minted by the core. They carry no
backend spelling, are unique across instances by construction, and never change
once assigned.

- The root and mount nodes are core nodes without map rows. Their IDs are
  assigned the first time they are seen. Renaming a mount in the config makes a
  new mount. The root is the only node without a parent.
- Every other node has a `node_ids` row `(id, instance, native)`, unique per
  `(instance, native)`.
- Every ID a backend exchanges with the core is public: in observations,
  deletions, mirror reads, ordering, and returned records. Native IDs appear only
  between a backend and its source. Backends translate where they already convert
  source data, so the core, ordering, enrichment, uploads, and HTTP work in one
  ID space.
- Identity must not merge unrelated objects. Whether an object keeps its ID
  across a rename or restart depends on the backend's identity strategy (see the
  backend specs). Otherwise it appears as a removal plus a creation.

`NodeIdMap`, bound to one instance:

- `public(natives)` looks up or mints IDs. Misses run one write-queue job that
  does `INSERT OR IGNORE` and then reads back, so concurrent calls for one new
  native ID get one public ID. Minting does not need the node's transaction,
  because a map row without a node is harmless and `gc` prunes it.
- `lookup(native)` never mints. Deletions and other events that must not create
  IDs use it.
- `native_of(id)` gives the native ID for calls to the source.
- `mint()` returns a fresh ID that is its own native ID, for backends that keep
  their own mapping. Its map row is written with the node's first applied
  observation, in that transaction, so a scan does not pay one
  transaction per new file.

Mappings never change, so lookups are cached in memory.

Map rows outlive their nodes. A delayed observation of a deleted native ID gets
the same public ID, so `ObservationOrder`, keyed by public ID, still rejects it.
Upload-conflict records that are never mirrored, and nodes under failed listings,
keep stable IDs for the same reason. A file withheld by a failed media probe is
recorded by the backend's source key, not by public ID. When its backend uses
`mint()`, nothing writes a map row until the file is published, so rescans of a
broken file do not leave rows behind. When its backend uses `public()`, as
Synology does, the map row exists from conversion onward. Only offline `gc`
prunes map rows that have no node.

### Routing

The root is rejected. A mount routes to its configured instance, because create,
upload, and move destinations are usually mounts. Any other ID routes to the
instance named by its map row, which also covers nodes not published yet. An ID
with no owner, or whose owner is not running, is unavailable (503). Moves between
instances are refused.

## Backend contract

A backend serves its mounts through the server's public backend contracts.
Handlers never build native references or catch native exceptions.

The core never imports a backend, a backend's source library
(`wcpan.synology`), or a client package, even though the built-in backends ship
in the same distribution under `wcpan.drive.server.backend`. It reaches a backend
only through the factory the config names (see Configuration and backend
factories), so a backend can be added without changing the core. Backends import
the core only through `types`, `lib`, and `exceptions`, like a third-party backend
inside or outside that namespace. `tests/test_backend_boundary.py` enforces both
directions; Ruff's banned-api rule in `pyproject.toml` bans client packages.

- `NodeOperations` creates a folder under a parent ID, and renames/moves,
  deletes, or downloads a mirrored `NodeRecord`. Create and update return the
  mirrored record; download streams exactly the bytes at an offset and length
  resolved against the mirrored size, and any exception while streaming is a
  failed stream. It writes the mirror through
  `CoreServices.observe` and raises the common errors:
  `OperationConflictError` (409, with the existing node when known),
  `OperationNotFoundError` (404, the node or target folder is gone from the
  source but still mirrored), `OperationStorageFullError` (507),
  `OperationUnavailableError` (503), `OperationNotSupportedError` (403), and
  `OperationInvalidError` (400).
- `Locate` fetches one node directly from the source: an observation, `None` if
  it no longer exists under a mirrored parent, or `LocateUndecidedError` when the
  source cannot tell.
- Upload sessions (offsets, expiry, requests) belong to the core. The backend
  controls staging and finalization through `Uploader` and `FileOpener`, so a
  backend can stage on the destination filesystem or take a file only whole
  (`SpooledFiles`). Conflict handling stays in the backend.

Synchronization is described in terms of four things, and a backend provides
only what its source supports:

- Invalidation: a node or scope needs to be read again (a notification is only a
  hint).
- Observation: metadata from a backend read, or a confirmed absence.
- Coverage: which listings completed and which scopes remain unknown.
- Progress: optional backend state, persisted only after the work it covers is
  durable (`CoreServices.enqueue_write` keeps it in write order).

Notifications, a replayable event stream, ordered source revisions, and
checkpoints are all optional; polling and full traversal are valid. Backends are
not modeled as a Synology scan with renamed fields.

## Backend framework

This section is the target design for the backend API. Implemented so far:
`CoreServices.observe` with `Changes`, `CoreServices.scans` with `Scanner`, `ScanPlan`, and
`Listing`, `CoreServices.hints` with `ChangeHandler`, `CoreServices.enrichment`,
`CoreServices.enqueue_write`, and the core-owned lifecycle (`BackendRuntime`,
`BackendLifecycle`), and `CoreServices.reconcile` with `ReconcilePass`. Both built-in backends
use only these. `MirrorStore` and `NodeSyncService` are no longer public, and
`tests/test_backend_boundary.py` keeps backends off `CoreServices.node_sync`.

### Goal

The core runs the sync flow. A backend extends small classes and fills in what is
specific to its source, using only public core modules. Every mirror change, whatever
produced it, travels through the same stages.

### Model

- A **fact** says what a node is, or that it is gone, as of one numbered source read.
- A **hint** says something may have changed. It carries no trustworthy node state, and
  a backend turns it into facts by reading the source.

#### Stages

| Stage | Turns | Workers | Allowed work | Order | Capacity | Failure |
|---|---|---|---|---|---|---|
| Hint | raw event into facts | 1 per backend | source and mirror reads | FIFO | unbounded | backend policy |
| Enrich | fact into enriched fact | several, shared | media probes, mirror reads | none | bounded | withhold and retry; an unavailable probe tool stops the server |
| Write | fact into committed rows | 1, shared | synchronous SQL only | FIFO | bounded | stops the server |

Enrichment is a step that can run two ways. `Changes.upserted` runs it in the caller,
because the caller needs the result and should not queue behind a scan backlog; it still
shares probe capacity with the workers. The scan runner submits file batches to the
enrichment queue. Directories need no probe, and a file whose dimensions can be reused
goes straight to Write.

#### Pipelines

| Source | Path |
|---|---|
| Webhook | hint -> source read -> enrich in caller -> write |
| Delayed webhook fetch | timer -> source read -> enrich in caller -> write |
| inotify | merged hint -> scan pass |
| Scan pass | listing -> directories (write) -> files (enrich queue or reused dimensions, write) |
| Subtree walk | listing -> same as scan, without removals |
| Create, move, delete | source change -> enrich in caller -> write -> API barrier |
| Upload | probe staged file if needed -> source change -> enrich in caller -> write -> API barrier |
| Media retry, single file (Synology) | timer -> source read -> enrich queue -> write |
| Media retry, by folder (filesystem) | timer -> scan pass over the parent folder |
| Checkpoint, backend state | write only |

#### Rules

1. **Work flows downstream.** A stage may wait on later stages, never on itself or an
   earlier stage. Hint may wait on Enrich and Write; Enrich may wait on Write; Write waits
   on nothing. Work going upstream, such as a media retry, starts from a timer or a
   non-blocking publish, and nothing downstream waits for it.
2. **The read number is taken at entry,** before the first source read that contributes
   to a fact, and is held until the write is applied or dropped. Queue position does not
   establish source-read order.
3. **Draining joins stages in order:** enrichment, then writes. It does not wait for
   retry tasks, delayed fetches, or hints.
4. **Bounded stages slow producers.** Publishing a hint never blocks.
5. **The pass lock sits above the stages.** Its holder may wait on Enrich and Write.
   Enrich and Write never wait for it. Nothing holding it may start a scan.

Scan removals and the exclusive lock span stages and are specified separately.

### What a backend supplies

Unchanged hooks: `NodeOperations`, `Uploader`, `FileOpener`, `WritableFile`,
`MediaSource`, `BackendSchema`.

```python
class Scanner[F](ABC):                  # one instance per scan pass or walk
    concurrent_roots = True             # False: traverse one root at a time

    @abstractmethod
    async def prepare(self) -> ScanPlan[F]: ...
    @abstractmethod
    def folder_id(self, folder: F) -> NodeId: ...
    @abstractmethod
    async def list_folder(self, folder: F) -> Listing[F]: ...
    @abstractmethod
    async def locate(self, node_id: NodeId) -> NodeObservation | None: ...

    async def skip(self, folder: F) -> bool:
        return False
    async def root_finished(self, root: F, complete: bool) -> None:
        pass
    async def pass_finished(self, complete: bool) -> None:
        pass

@dataclass
class ScanPlan[F]:
    scope: set[NodeId]                 # effective reconcile scope, after normalizing
    roots: list[F]                     # folders to traverse
    preserved: list[NodeId]            # in scope but unusable

@dataclass
class Listing[F]:
    seen: list[NodeId]                 # every child this listing accounted for
    changed: list[NodeObservation]     # subset to write
    subfolders: list[F]                # visit next
    keep: list[NodeId]                 # deliberately not visited; subtree preserved

class ChangeHandler[E](ABC):
    continue_after_failure = False     # True: log a failure and take the next event

    @abstractmethod
    async def handle(self, event: E) -> None: ...

class BackendRuntime(Protocol):
    async def startup_scan(self) -> None: ...  # start change sources, then scan
    def change_handler(self) -> ChangeHandler | None: ...
    async def run(self) -> None: ...           # started after the startup scan
```

### What the core provides

```python
core.mirror       # read-only node access, plus read(fn) for backend tables
core.ids          # NodeIdMap: public() persists its row at once; mint() is provisional
                  # until the node's first applied observation
core.media        # register, probe_upload
core.enrichment   # drain, scope, retry_failures(locate), retrying(locate),
                  # retrying_with(callback), prune_failures(before)
core.hints        # publish(raw_event), later(key, delay, fn)
core.scans        # await run(scanner), await walk(lister, root)
core.observe(exclusive=False)   # the one way a backend produces facts
await core.enqueue_write(fn, *args)   # fn(con, *args) as one ordered transaction
await core.writes_landed()      # every write queued so far has landed
await core.bind_schema(schema)
```

`NodeSyncService` and `MirrorStore` are not public. `CoreServices.node_sync` remains for the
core's own use.

#### Producing facts

```python
async with core.observe(exclusive=True) as changes:    # read number taken here
    result = await source.move(...)
    record = await changes.upserted(observation, strict=True)
    await changes.removed(old_id)
# every fact from this block has landed
```

- `upserted` enriches in the caller, queues the write, and returns the enriched record.
  With `strict=True`, a failed probe raises inside the block.
- `removed` queues today's subtree removal, including its ordering check and schema
  cleanup.
- The block must start before the earliest source read its facts depend on.
- A non-exclusive block does not wait on exit, so a hint handler keeps today's throughput.
  `await changes.landed()` waits with a barrier for writes queued so far, not for later
  writers. API visibility stays with the router's barrier after each mutation.
- `exclusive=True` holds the pass lock and runs that barrier on exit, including when the
  block raises, before releasing the lock. A scan must not be started inside the block.
- A barrier fails promptly if the write worker stops. If the block already raised, its
  exception propagates with the barrier failure as context, and the lock is released.
- The block is an ordering scope, not one transaction. Each write is its own transaction.
- Source first, then mirror. No transaction is open during a source call. A crash between
  them is repaired by the next scan, and clients resolve lost responses by sync and hash
  comparison.
- Node-tied backend state uses `BackendSchema` hooks in the node's transaction. Checkpoints
  and other progress use `enqueue_write`, ordered after the work they cover.

Read-only commands receive a core without `scans`, `hints`, `observe`, or `enqueue_write`, with
provisional IDs and a schema check that does not write. To report what would change, they use
`core.reconcile(..., dry_run=True)`. This differs from a backend's
own `read_only` setting: a read-only filesystem source still writes the mirror and probes
while serving.

A backend whose comparison does not fit `Scanner`, such as Synology's `backfill`, drives the
pass itself with `core.reconcile(scope, locate=..., dry_run=...)`. Each `listing()` numbers one
read and yields `ReconcileWrites` (`inserted`, `updated`), or `None` in a dry run, and the pass
ends with `removal_roots()` or `commit_removals()`. Offline, the command runs enrichment
workers in `core.enrichment.scope()`.

### Hint stage

`publish` does no reads and accepts a raw backend event. One consumer per backend calls
`handle(event)` for one event at a time. Everything in `handle` runs in source-code order,
so read-before-write and read-after-write are ordinary sequencing. Facts come from
`core.observe`. A value read before the block must not become an observation.

`later(key, delay, fn)` runs `fn` as its own supervised task after the delay. A newer call
with the same key cancels only a pending timer, never running work. `fn` takes its own read
number when it starts. It does not block the consumer and does not count toward readiness.

Synology folder move:

```python
async def handle(self, event):
    # Old source and destination membership, read before this event's write.
    skip_walk = await self._move_stays_inside_mounts(event)
    async with core.observe() as changes:
        match await self._fetch(event):
            case OutsideMounts(node_id):
                await changes.removed(node_id)         # found by ids.lookup, never minted
            case NodeObservation() as fact:
                await changes.upserted(fact)
            case None:
                pass
        await changes.landed()
    root = await self._moved_root(event)
    if not skip_walk and root is not None:
        await core.scans.walk(self._subtree_lister(), root)
```

A directory-create handler also calls `landed()` before returning, so the next hint can
resolve the new parent. Plain file removals and upserts do not wait.

Filesystem:

```python
async def run(self):
    while True:
        await watcher.wait()
        await asyncio.sleep(settle_delay)
        dirty, overflow = watcher.take()
        core.hints.publish(FolderChanges(dirty, overflow))
        await core.hints.join()        # changes during this pass merge into the next

async def handle(self, event):
    await core.scans.run(self._scanner(None if event.overflow else event.dirty))
```

### Scan runner

A `Scanner` instance serves one pass or walk, and the caller creates it. Passes run one at
a time, whether started by startup, a handler, a timer, a media retry, or
an API operation. For each pass the core:

1. Takes the pass lock, creates a Scanner, and calls `prepare()`. The filesystem swaps a
   vanished or replaced dirty folder for its parent here. The core opens one reconcile
   session over `plan.scope`, before any listing, and preserves `plan.preserved`. Values
   read in `prepare` never become observations.
2. Traverses the roots, concurrently unless `concurrent_roots` is false. Each work item
   carries its root, and completeness is tracked per root. For each folder, one read number
   covers `skip` and `list_folder` and is held until resulting writes are applied.
3. Records `seen` and `keep`, submits directories, then files. File enrichment waits for
   directory writes when there are files. Descending does not wait: `F` must carry what is
   needed to list the folder. If `list_folder` raises, its subtree is preserved and its root
   is incomplete. `skip` returning true preserves the subtree without making the root
   incomplete. A `skip` that raises counts as a failed listing.
4. Confirms and commits removals through `locate`. Computing candidates drains enrichment,
   so files found by this pass are published or withheld before the lock is released;
   otherwise a later pass could mint a second ID for the same new file.
5. Drains, calls `root_finished` for each root, calls `pass_finished`, and releases the lock.

`pass_finished(complete)` is true only if every root is complete and `plan.preserved` is
empty. If `prepare` raises, the pass reconciles nothing. A failed finish hook fails the pass.
A failed pass raises to its caller. Before releasing the lock, a failed pass drains work it
already submitted, so the next pass cannot mint a second ID for a file still being enriched.
The drain fails promptly if a worker has stopped. Only holds the pass still owns are released
by it; holds handed to a queue stay with their jobs. Cancellation at shutdown does not drain.

`walk(lister, root)` lists a subtree without the pass lock, reconcile session, removals, or
finish hooks. A lister needs only `list_folder`, and returns every convertible child
in `changed`, without incremental filtering. Directory writes land before descending. File
enrichment is submitted and not awaited. A failed listing raises.

`list_folder` installs any watch before its first source read. Synology sets
`concurrent_roots = False`, keeps per-mount state keyed by root, handles the mount level in
`list_folder`, and keeps the watermark read before the scan as its checkpoint. It runs one
pass, at startup, so it records results in `root_finished` and, once the pass returns, retries
failed media and then queues the checkpoints. The filesystem's `pass_finished`
prunes failure records after a full pass, using the pass start as the cutoff.

The filesystem's hourly rescan, media retry rescans, and name-conflict lookup call
`core.scans.run` directly. The conflict lookup runs outside any exclusive block.

Filesystem create, update, delete, and final upload publication enter
`core.observe(exclusive=True)` before their validation reads, so a scan cannot mint another
ID for the new entry. Staging, transfer, upload probing, conflict scans, and partial-delete
repair stay outside the block. Downloads need none.

### Lifecycle

Startup runs per backend as supervised background work, as `_flip_ready` does today. The HTTP
listener is already up, so backend routes such as webhooks work during the scan, while ordinary
API handlers answer 503 until every backend is ready.

0. Media retries are registered while the backend is entered, before the startup scan, and stay
   registered through serving. They are keyed by source key, not `NodeId`.
1. Published hints are held from the start.
2. The core awaits `startup_scan()`, which starts change sources and scans, normally through
   `core.scans.run`, and does anything that must follow the pass.
3. The hint consumer and `run()` start.
4. The core waits until every accepted hint has been fully handled, as `queue.join()` does
   today, and enrichment has drained. The backend is then ready. `later` work does not count.

Synology hints wait in the core queue during the scan, so readiness includes them. The
filesystem watcher keeps changes in its dirty set, and `run()` publishes after the scan, so
they do not delay readiness, as today.

Every started task is supervised. On shutdown, runtime tasks, workers, and retries are cancelled
and awaited before sources close. Hint failure policy is per backend: stop the server (Synology)
or log and continue (filesystem). It covers source and handler errors only.

### Testing

`wcpan.drive.server.testing` is the public way to test a backend: `backend_harness()` runs a
core over a temporary database, and `add_instance` returns the `CoreServices` and
`BackendContext` the server would give that instance, with fixed mount IDs and fake media
probes. The filesystem backend's tests use only it and the public modules, which
`tests/test_backend_boundary.py` enforces. Backend source must not import it.

Framework rules are tested with an in-memory fake backend and a real temporary SQLite mirror,
without HTTP. Ordering is forced with gates, never sleeps. The core accepts an injected clock or
scheduler for `later`, settle windows, periodic scans, retry backoff, and prune cutoffs, and
exposes completion barriers for hints and passes. Real inotify delivery, filesystem identity,
NAS watermarks, and crash durability stay in backend integration tests.

## Reconciliation and ordering

### Observation ordering

Mirror writes are ordered by when the backend read that produced them began, not
by when their result reaches the writer.

- Every backend read takes a sequence number when it starts: a listing, a
  single-node fetch, or a notification-reported removal.
- An upsert applies only if no observation from a later-started read has been
  applied to that node. A removal applies only under the same condition.
- Sequence numbers older than every outstanding read are pruned, so the
  bookkeeping stays bounded.
- A read stays outstanding while a write or enrichment derived from it is
  pending. Each such hold has exactly one owner at a time and is released
  exactly once, also when the work fails or is cancelled before reaching the
  next queue (both queues are bounded, so handing work on can wait); a leaked
  hold would keep the read outstanding and stop pruning. Work abandoned at
  shutdown keeps its holds, since nothing runs after it.

A notification is delivered after the change it reports, so the read it triggers
starts after that change and sees it, while an overlapping read that started
earlier may be stale. A change during that read produces another notification
and a later read. Correctness therefore depends on notifications not being lost
silently; a backend that can lose them needs repair passes.

A sequence number is not proof of source freshness, and the single writer
serializes database writes, not observations. Enrichment is ordered the same way,
so stale derived metadata cannot overwrite a newer observation or resurrect a
removed node.

The ordering lives in memory in one process, so only one process may write the
mirror. Discovery, notifications, API writes, and scoped repair run inside the
server. Backfill, `gc`, `squash`, `drop`, and `rebuild` are offline commands that
run only while the server is stopped.

### Mutation visibility

An API mutation answers only after its mirror writes have landed, so the next
request sees it (`packages/protocol/SPEC.md`). Public IDs are minted, so the
mirror is the only way to address a node: handlers load records from it, and
backends resolve parents through their locators, which commit with the node.
Answering before the write lands made a create followed by a call on the new
node fail with 404 or 500.

- A backend's mutation returns once its mirror writes are queued: `upsert`
  enriches before queuing, and `delete` queues directly.
- The core then waits on a barrier, an empty job queued after them. The queue is
  first in, first out, so the barrier completes once the mutation's writes and
  everything queued before them have landed. Joining the queue instead would also
  wait for writes queued later, which a running scan can keep adding.
- A write that ordering rejects, because a later read already observed the node,
  still passes the barrier; the mirror then shows that later observation.
- The wait includes jobs ahead in the shared queue. Jobs only run SQL (see Write
  queue), so this costs queueing, not probing or transfers.

### Reconciliation sessions

Startup scans, rescans, scoped repair, and backfill share one mechanism,
`ReconcileSession`. A pass over a scope collects what
listings saw and which folders they could not cover, and decides removals once,
at the end.

1. Missing from an incomplete listing does not mean deleted. A folder that could
   not be listed (`preserve`) keeps its whole mirrored subtree.
2. A listing is not an atomic snapshot of the tree: a node can move into a folder
   already listed and out of one not yet listed.
3. A mirrored node is a removal candidate only if no listing saw it, no uncovered
   folder contains it, and no read that started after the pass began touched it.
4. Each candidate subtree root is fetched directly (`Locate`) before removal. A
   node found elsewhere is recorded under its new parent; an undecided lookup
   keeps it; `None` removes it. What counts as undecided is the backend's call:
   backend-specific absence and error rules are in the backend specs.
5. A backend confirms its source is present before declaring its root covered,
   so an unmounted source exposing an empty directory cannot erase the mirror.
6. A pass can run without a writer: listing, coverage, and `removal_roots` need
   none, so a dry run (Synology `backfill --dry-run`) reports candidates without
   writing. `commit_removals` fails in a dry run.

Database check-then-write races are handled by the single writer and checks
inside each transaction; stale external reads are handled by targeted validation
(ordering, `Locate`), not by a general locking or version framework.

## Change log and cursors

The wire contract for cursors and changes is in `packages/protocol/SPEC.md`.

- Every public change to a node appends a row to `changes` (`change_id`, node,
  removed or not) in the same transaction as the change. In production, nodes
  are written only by applying observations, which append a row only when the
  node row actually changes; locator-only and no-op updates append nothing.
  `MirrorStore`'s seeding helpers bypass this and are test-only.
- Public cursors refer only to committed changes. Backend checkpoints refer only
  to backend discovery, and scan coverage is separate from both.
- The cursor is `<epoch>.<change_id>`, and parsing belongs to the HTTP boundary.
  `epoch` is a random value in `metadata`, set when the database is created and
  replaced only by `squash`, so it stays fixed while the server runs. Opening a
  database that predates the epoch adds the row without changing the schema
  version.
- A cursor from another epoch, or past the current maximum `change_id`, is stale
  (410). The second check catches an older copy of the database restored under
  the same epoch, without needing to change the epoch on restore.
- A page reads up to `max_size` rows after the cursor and keeps the latest row
  per node; the returned cursor is the last row read, so no row is skipped. An
  upsert row whose node is gone by the time it is read is left out, because a
  removal row for it follows.
- `squash` (offline) replaces the epoch and rewrites the log as one upsert per
  node except the root, dropping tombstones. Clients with old cursors get 410
  and replay, so they never miss a dropped tombstone.

## Publication: checksums and media

A node is published only when its metadata is complete as far as the server can
make it. An update to a visible file that is not yet complete is withheld, and
the last complete version stays visible until the update is published as one
change. Clients never see a partially updated node. Removing and re-adding the
node instead was rejected because clients would see a deletion.

While the last complete version stays visible, the backend's content may already
have changed, so a download can disagree with the visible digest. Verification
then fails and the caller retries.

### Checksums

- The backend supplies the checksum and its algorithm identifier (`hash`,
  `hash_algorithm`); the core does not compute or validate them. A file without a
  checksum has both fields empty, and is published that way.
- Whether a backend can publish a file without a checksum is its own policy (see
  the backend specs).
- Algorithm choice is per file. It is not tied to mounts or directories.

### Media probing

Images and videos are probed for `width` and `height` with the system
MediaInfo executable, reading the backend's bytes over a
loopback HTTP service. Probing happens before an observation is applied. A
failed probe never removes a node already in the mirror.

- Each probe runs `mediainfo --Output=JSON --File_TestContinuousFileNames=0
  URL` as a subprocess, with a bounded number at once and an overall deadline;
  it is killed and reaped on timeout, cancellation, or shutdown. It runs
  without the server's `*_proxy` variables: its libcurl honours them even for
  loopback, which would send the probe URL and token to a proxy and hang the
  probe. The server's own HTTP clients ignore them too. MediaInfo's
  own reader decides which ranges to fetch, so a probe may read a whole file:
  there is no byte or request budget, since valid formats can require reads
  across most of the file.
- The URL names an open `MediaSource` on the probe source service
  on a separate listener on loopback, on an ephemeral port,
  never a route of the main app, since the subprocess shares the server's
  network namespace. A URL holds an opaque token valid for one probe; the
  service never takes paths or upstream URLs, and does not log requests, since
  the path is the token. Ranges follow the same semantics as downloads.
  MediaInfo closing a response to seek cancels that request's handler, so a
  stalled upstream read stops at once instead of timing out and failing the
  probe. Ending a probe revokes its token and
  cancels requests still being served before the source is closed. Source
  errors other than a `MediaSourceError`, while reading or rechecking, are
  recorded and logged by type only, since upstream errors can carry URLs with
  credentials.
- MediaInfo is required before the first probe (at server startup, and before
  an offline command's first probe): its version must meet the compatibility
  requirements in `README.md`, and a small PNG must probe correctly through
  the service. A missing or incompatible executable makes probing unavailable,
  which stops the server rather than failing every file.
- A `MediaSource` has a known size and gives positional streams:
  `stream(offset, length)` yields exactly that many bytes, in bounded chunks, or
  raises. Streams are independent and may overlap; a remote source makes one
  upstream request per stream, and closing a stream early stops it. Not a
  seek-then-read cursor, because HTTP ranges are independent requests; not
  `read(offset, size) -> bytes`, because each remote read costs one upstream
  request, so chunked reads would cost one each.
- MediaInfo's exit status and output do not establish success: it has
  returned dimensions with exit 0 after an interrupted transfer, sometimes with
  nothing on stderr. The probe session records every read the source could not
  complete (an error, a stream ending short within the file's size, or a read
  stalled past its timeout), and any record fails the probe, whatever MediaInfo
  printed. MediaInfo closing a response early to seek elsewhere is normal, and
  the parser reading only part of the file is not a failure: dimensions often
  come from the first few kilobytes, and what it never asks for is never
  checked. After the subprocess exits, the source must still match the
  observation.
- Exception: a source offering `local_fd()` (the filesystem backend's, on
  Linux) is read by MediaInfo as `/dev/fd/N`, with no session. A local I/O
  error that MediaInfo takes for the end of the file is then not detected;
  truncation and rewrites still fail the recheck. Its deadline is always
  `limit`, since no read of the server's is in flight.
- Failure kinds: a failed or stalled read, a changed source, a nonzero exit or
  crash, output that is not MediaInfo's JSON, or an `E:` error line (for
  example a URL it could not read, after which it still exits 0) are
  `transient`. No video or image track, `media: null`, or a zero width or
  height is `invalid`, so probed dimensions are always complete; the last video
  or image track is used. The deadline passing is `transient` if a read was in
  flight or had timed out, and `limit` if MediaInfo was only parsing. A missing
  executable is not a per-file failure (see above).
- A backend registers how to open its sources while it is connected
  (`MediaProbing.register`); a probe of a backend with none fails.
- In-process pymediainfo, fed one read at a time, was replaced: it needed fixed
  buffer sizes and fetch budgets, and its bundled libmediainfo failed some WMV
  files.
- A published file whose size and hash are unchanged, and whose stored dimensions
  are complete (both nonzero), keeps them without a probe (rename, move,
  metadata change).

Only a file not yet in the mirror is withheld. A file already in the mirror keeps
its stored dimensions when a probe fails, even if they are zero, because the
upsert never overwrites nonzero dimensions with zero:

| Case | Behavior |
| --- | --- |
| New file, probe fails | Withheld. A failure record with the failure kind is kept under the backend's source key. |
| Published file, content changed, probe fails | The new metadata is published with the last successful dimensions; no failure record. |
| Upload without complete client media info, invalid media | Probed before the file is stored (in the spool or staging file), so nothing is stored; the upload fails with `422 invalid_media` and the spool is discarded. |
| Upload without complete client media info, other probe failure | The upload fails as retryable (503). A resumable session keeps its spool and can be finished again; nothing reached the backend. |
| Upload with complete client media info | Not probed; the client's dimensions are used. |

Whether an upload is media is decided from the client's media info, or else
from the name, as the filesystem classifies files. Synology classifies by
content; when it calls media a file whose name did not say so, the stored file
is probed over the API like any observation: invalid media is deleted (best
effort) with `422`, and any other failure leaves it to be observed again.

A changed file is not withheld because its next observation probes it again,
and withholding it would hide content that is already visible.

The failure kind decides when a withheld file is probed again:

- Transient (a failed or timed-out read, a file that changed mid-probe,
  MediaInfo failing or crashing, the deadline passing while waiting for the
  source): retried with backoff while the backend
  serves, with bounded exponential backoff.
  Each attempt re-observes the file under a fresh read, through the backend's
  `retrying` registration. Timers live in memory, keyed by instance and source
  key; a probe of the file that starts (from a scan, a webhook, the startup
  pass) cancels the pending timer, and applying the file, pruning its record,
  or the backend leaving cancels it too. A timer that fires while its previous
  attempt still runs fires again once it ends. A retry only submits the file,
  so an observation submitted meanwhile can probe it once more; ordering keeps
  the newer result.
- Invalid media (no video or image track, or a zero width or height): no retry until the content changes, which brings a new observation,
  or the backend's startup pass.
- Probe limit (the deadline passed while MediaInfo was parsing): inconclusive
  rather than invalid, and retried like invalid media. Records left by the
  retired byte and read budgets are read as before and retried by the same
  startup passes.

A MediaInfo that cannot be run makes probing unavailable, which stops the
server (or fails the command) instead of failing each file. Backoff retries
never hold readiness open.

Failure records (`media_failures`) are keyed by instance and a backend-chosen
source key, not by public ID, so the record itself keeps no public ID. Whether
an ID map row exists depends on the backend (see Public node IDs). An applied observation clears its record; removing a node clears the
record keyed by its native ID. How a backend retries and prunes records depends
on its discovery (see the backend specs). `backend NAME pending` lists an
instance's records; each backend's README describes its output.

Probes and the enrichment writes they produce go through `ObservationOrder` like
any other observation, and a recorded failure counts as an applied observation,
so a slow probe cannot overwrite a newer observation or failure, or resurrect a
removed node. Ordering does not prove the file stayed unchanged while
it was hashed or probed. A backend must check that itself (see the filesystem
spec) and withhold a result that no longer matches the source state it examined.

An older database with the earlier `pending_enrichment` table (keyed by public
ID) is converted in place without a schema version change. Records whose native
ID differs from the public ID (Synology) carry over under the native ID. The rest
(filesystem records keyed by discarded in-memory IDs) are dropped, together with
the ID map rows only they kept alive.

### Blocking I/O

SQLite work runs on the core's executor (`OffMainService`); media parsing runs
in MediaInfo subprocesses (see Media probing). Backend blocking I/O and bulk content reads belong on pools the
backend owns, so they cannot starve database work; the filesystem backend does
this. A timeout does not cancel a blocked thread, so a backend must bound its
pools.

## Configuration and backend factories

The core owns the versioned server config schema; usage is in `README.md`.

- The core parses `server`, backend instance names and `type`, and mounts (name,
  instance, `source` as a string). Each backend checks its own settings and
  validates its mounts' sources. The core only joins a source with node names
  (`/`-separated) to build a source path.
- A mount name is one path component: not empty, `.`, or `..`, and without `/`.
  Nested virtual paths are out of scope.
- An instance name is the persistent identity of a source instance (see Backend
  instances). The config holds no discovery state: locators, handles,
  checkpoints, and IDs live in the database.
- The server has no public URL setting. Only a backend that needs one (Synology
  webhook registration) takes a callback URL in its own settings, so a
  filesystem-only deployment needs none.
- Only version 1 is accepted. `server.upload_tmp_dir` is rejected instead of
  guessing which backend it belongs to.

A backend is an async context manager factory named by its import path
(`module.function`), called as `factory(context, **settings)`.

- Settings are the factory's keyword-only parameters, checked by
  `wcpan.drive.plugin.check_settings` before the factory runs (see
  `packages/plugin/SPEC.md`). A bad setting fails startup instead of reaching the
  source.
- `BackendContext` carries the instance name, the mounts it serves, the core
  services (mirror reads, ID map, media probing, and the framework services), a route
  registry, and the command's `access` (`None` while serving; `offline` is
  true otherwise). The core services are `CoreServices`, or
  `ReadCoreServices` for a read-only command: the same field names typed as
  their read sides (`MirrorReader`, `NodeOrdering`), so code that does not
  narrow can only read. Work that writes narrows with `writing(core)`, which
  fails for a read-only command. Schemas are bound through
  `core.bind_schema`, which registers when writing and only checks when
  reading.
- The probe runner and its source service start before the backends and stop
  after them; the metadata
  workers start after the backends and stop before them, so no probe outlives
  the connection it reads through. Offline, no metadata workers start with the
  backends; a command that queues enrichment (Synology `backfill`) runs them
  in `core.enrichment.scope()` inside its connection. A normal exit
  drains enrichment and its writes, a wait that also ends when the write
  worker task ends. An error cancels running probes and re-raises without
  waiting for queued work, so running the command again redoes it; the
  startup scan does not, since it skips nodes unchanged since its resume
  point.
- Shutdown does not protect queued work. The mirror only copies its backends,
  and a backend saves its resume point through the write queue, after the
  writes it covers, so whatever a crash or SIGTERM drops, the next start
  observes again. An offline command that stops early is run again instead. Cleanup on cancellation is for a server that keeps running:
  a cancelled request must not leak a held read or close a file a thread
  still uses.
- The write worker marks a job done only once it has landed: a job that
  fails or is interrupted stays unfinished, so the queue's `join` never
  reports a write that did not happen. `NodeSyncService.flush` and the other
  waits on queued writes also stop when the worker task ends, raising
  `WriterStoppedError` instead of waiting forever.
- Maintenance commands (server stopped) are parsed before anything starts. A
  factory carries its parser (`@backend_commands(parse)`), which turns the
  arguments after `backend NAME` into a `BackendCommand` declaring its database
  access (`READ` or `WRITE`) and whether it probes media; a `READ` command
  cannot probe. `--help` and argument errors exit there, before logging, the
  database, or the source is touched.
- Offline, a factory must not connect or start background work.
  `run_command` connects only if the command needs it.
- A `READ` command gets `ReadCoreServices` built by `managed_read_core`: the
  mirror opened through `read_only_dsn`, the core schema and the instance
  checked without changes (`check_schema`, `lookup_instance`,
  `check_backend_schema`), `ReadOrdering`, `ProvisionalNodeIds` (IDs for
  unmapped native IDs live only for the command), and media probing without
  a runner; no queues, workers, or probe runner. It refuses a missing
  database, a non-empty WAL, a database not in WAL mode, or pending core
  compatibility changes, including missing core tables or indexes (naming
  `checkpoint`), and an instance or backend schema the server has not set up
  (naming a server start). Backend tables and indexes are not inspected: a
  recorded backend schema version means its DDL already ran.
- The repository's `tests/test_read_only_commands.py` runs every command a
  backend declares `READ`, with and without a zero-byte WAL: in a writable
  directory the database, `-wal`, and `-shm` must keep their bytes, and with
  the directory and those files read-only any write attempt fails the
  command. A new `READ` command must be added there.
- A `WRITE` command gets the usual core, with the probe runner only if it
  declares `probes`. Offline, queuing enrichment outside `enrichment_scope`
  raises, since nothing else runs the metadata workers.
- The aiohttp router is frozen before factories run, so the core owns one route,
  `/api/v1/backends/<name>/<path>`, and dispatches to handlers the instance
  registered.
- Backends ship no scripts. Their factories are registered under the
  `wcpan.drive.server.backend` entry point group, which `init` uses to generate a
  starting config. A third-party package registers its own the same way.

## Backend instances

The registry `backend_instances` records, per instance: its config name, its key
(an integer, assigned on first sight and stored in the `instance` column of
backend tables, the ID map, and media failure records), and its bound backend
schema.

- The key, not the name, tags rows, so a rename touches one row. There is no
  rename command, because it could not change the config: to rename an instance,
  stop the server and change its `name` in `backend_instances` and in the config
  (the instance and its mounts' `backend`) together. Renaming only the config
  makes it a new instance, and the old name is reported as unconfigured.
- Keys use `AUTOINCREMENT`, so a dropped instance's key is never reused and a
  row a drop missed cannot attach to a later instance.
- An instance that is registered but no longer configured is reported at startup
  and kept until dropped, so a config typo does not discard a mirror. Its nodes
  stay in the mirror, and content operations on them return 503.
- Clients do not cascade a removal to descendants, so every removal writes one
  tombstone per node. None of the commands below needs a cursor reset.
- `backend NAME drop` (offline) removes the instance's nodes with tombstones, its
  media failure records, backend rows, map rows, and registry entry, in one
  transaction. It refuses a configured instance.
- `backend NAME rebuild` (offline) does the same but keeps the map rows and
  registry entry. Initial discovery then runs on the next start, and items with
  stable native IDs come back under their old public IDs. Map rows a rebuild
  leaves unused are pruned by a later `gc`, so running `gc` before discovery
  finishes loses only ID continuity, not data. It refuses an unconfigured
  instance.
- The core handles both commands before the backend sees its arguments, so they
  work without loading the backend.
- Mount reassignment runs at every start, before backends are entered: children
  of a mount owned by another instance are removed with their subtrees and
  tombstones. The new owner discovers the mount with new public IDs; the mount
  node and its ID stay.
- `gc` (offline) is one pass. A node is reachable from the root through its
  parents; under a configured mount, only the owning instance's nodes are
  reachable; a mount not in the config keeps everything below it. Unreachable
  nodes are removed with tombstones, then map rows with no node are pruned.
- `checkpoint` (offline) applies the core schema's same-version compatibility
  changes, then runs `PRAGMA wal_checkpoint(TRUNCATE)`, failing if another
  connection keeps the WAL busy. It refuses to create a database. It prepares
  a database for read-only commands, which neither migrate nor read the WAL.
- Read-only offline opens use `mode=ro&immutable=1`
  (`wcpan.drive.sqlite.read_only_dsn`). `mode=ro` alone can
  create or update `-wal`/`-shm`, even on a clean database, and fail in a
  read-only directory; `immutable=1` writes
  nothing but ignores the WAL, so committed transactions left in it by a
  crash would not be seen. A read-only command therefore refuses a non-empty
  `-wal` (`has_pending_wal`) and names `checkpoint`; a zero-byte `-wal` and a
  leftover `-shm`, as `checkpoint` leaves them, are fine. `immutable=1` is safe
  only because no writer runs while the server is stopped. Both resolve
  symbolic links, as SQLite does to name the WAL files, so a symlinked
  database is checked against its real `-wal`. An immutable connection
  reports the rollback journal, so WAL mode is read from the file header
  (`uses_wal`).

Readiness is global: one failed initial discovery keeps the whole server unready.

## Why the database is shared

Separate backend files would isolate corruption and allow independent restores,
but would lose the single change order and atomic commits of nodes with their
locators. They would also require cross-file parent checks and coordinated
backups. SQLite `ATTACH` does not preserve atomicity across files in WAL mode.

Keep one database unless measured size, write contention, or recovery needs
justify those costs.
