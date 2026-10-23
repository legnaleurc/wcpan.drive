# Drive client/server workspace and extraction plan

Status: the uv workspace and namespace migration are initialized at version
`0.1.0`. The flat `hash`/`hash_algorithm` model and per-node hasher selection are
implemented in this repository (protocol, server, and all client packages; client
service API version 6). Stages 1 and 2 are complete. duld migration and the
generic backend architecture below remain planned. The Synology adapter
is extracted from the generic server at the end of stage 3, before the filesystem
prototype.

## Objective

Separate the HTTP client, generic mirrored file server, and Synology backend.
Validate the server boundary against a Linux local filesystem backend so that
the extracted server does not retain Synology synchronization assumptions.

The public mirror uses stable, opaque node IDs. Backend locators, event sources,
and discovery progress remain private to each backend.

## Packages and dependencies

Use one uv workspace under the `wcpan.drive` umbrella. Each member is separately
published, with its own dependencies and version. The root is not published.

| Distribution | Python namespace | Responsibility |
| --- | --- | --- |
| `wcpan-drive-client` | `wcpan.drive.client` | Client framework, public contracts, model, exceptions; constructors in `client.api` |
| `wcpan-drive-client-google` | `wcpan.drive.client.google` | Google provider |
| `wcpan-drive-client-remote` | `wcpan.drive.client.remote` | HTTP server provider and client-model adaptation |
| `wcpan-drive-client-crypt` | `wcpan.drive.client.crypt` | Encryption middleware |
| `wcpan-drive-client-sqlite` | `wcpan.drive.client.sqlite` | Client metadata cache |
| `wcpan-drive-client-cli` | `wcpan.drive.client.cli` | Client command line |
| `wcpan-drive-protocol` | `wcpan.drive.protocol` | Lightweight HTTP wire models and serialization |
| `wcpan-drive-server` | `wcpan.drive.server` | Mirror, HTTP server, reconciliation, and backend extension contracts |
| `wcpan-drive-server-synology` | `wcpan.drive.server.synology` | Synology adapter |
| Future `wcpan-drive-server-filesystem` | `wcpan.drive.server.filesystem` | Filesystem backend; not yet shipped |

Client and server roots are namespace packages with explicit public modules and
private implementation. Neither needs a separate core distribution initially;
extract contracts only if dependency or release needs justify it. The protocol
package depends on neither runtime. The remote provider depends on the client
framework and protocol, never on the server. The server must eventually depend
on neither the client framework nor concrete backends. The current initialization
removes client-framework imports from the server but still has Synology-specific
orchestration; the adapter distribution supplies the runnable server command.

Breaking migration is accepted: retire old distribution/import names directly,
without compatibility wrappers. Newly named distributions begin at `0.1.0`.
Existing external consumers, including duld, require coordinated migration.

### Independent feed protocol

`wcpan-drive-server` is an independent file service. It owns its node model, HTTP payloads,
capabilities, metadata availability, and errors. Its payload may initially retain
the current shape for compatibility, but need not follow `wcpan.drive.client` types
or evolve with them.

There are two distinct adaptations:

```text
Synology / filesystem
    -> backend adaptation -> feed model and HTTP API
    -> drive client adaptation -> wcpan.drive.client model
```

The client maps feed nodes, changes, capabilities, and errors into the drive
interface, as other drive clients adapt their providers' APIs. Similar fields do
not require a shared runtime type or dependency. Replace existing server imports
from `wcpan.drive.client` with feed-owned definitions during extraction.

## Ownership boundaries

### Generic server

- Normalized node metadata, virtual root/mount presentation, and opaque IDs.
- SQLite mirror, public change log, tombstones, feed cursor validity, and GC.
- Reconciliation sessions, coverage tracking, and conditional removal.
- Atomic application of observations and backend persistence updates.
- Generic HTTP handlers and translation of common application errors into HTTP.
- Upload-session offsets, expiry, and request handling.
- Readiness state, lifecycle coordination, and a private SQLite execution facility.
- Optional derived metadata work, with conditional updates against its input version.

### Backend

- Identity rules and locator meaning, including reverse lookup from native events.
- Listing, metadata retrieval, create, rename/move, delete, download, and upload.
- Native error translation into common server errors.
- Discovery strategy, notification registration, debounce, and recovery from loss.
- Native checkpoint interpretation, serialization, comparison, and reset rules.
- Source validation and backend-specific path, name, mount, and symlink policies.
- Backend-specific routes, configuration, credentials, and I/O execution resources.

The server owns generic readiness reporting; the backend reports synchronization
progress and degraded coverage. The Synology webhook route is registered by the
Synology backend through an application extension hook.

## Node identity and backend state

Remove `mutable_id` from the normalized server node model. Keep only opaque node
IDs, parent IDs, and public metadata. The core must not parse Synology permanent
links, Linux handles, or backend-specific ID prefixes.

Identity must not falsely merge unrelated objects. Continuity across a rename or
restart depends on the backend's documented identity strategy. Object replacement
or an unresolvable identity transition may produce removal plus creation.

Backend state lives in the same SQLite database as the mirror. The backend owns
its schema and semantics, while the server supplies a transaction-bound write
facility. Begin with concrete Synology locator/checkpoint tables; avoid designing
a universal state-blob framework before both backend prototypes demonstrate the
requirements. Indexed reverse lookup must remain possible.

For Synology, store `file_id` in a locator table associated with the public node
ID, with an index for webhook reverse lookup. Store the source path and `max_id`
in a checkpoint table scoped to the backend instance and mount. Retaining the
source path allows checkpoint invalidation when the source changes.

Do not introduce a generic `mounts` table containing only an ID without a concrete
need. Mounts can remain configuration-driven, with their virtual nodes in `nodes`
and their Synology state in backend-owned tables. A generic mount table is only
needed if generic persistent mount attributes emerge.

Required persistence behavior:

- Commit a node observation and its identity/locator binding together.
- Locator-only changes do not emit public metadata changes.
- Delete or invalidate obsolete bindings consistently with node lifecycle.
- Scope identity and checkpoint state to a source instance; changing a source
  configuration must not accidentally reuse another source's state.
- Keep virtual IDs and backend IDs collision-free through an explicit namespace
  boundary, rather than assumptions about a backend's ID spelling.

### Writer queue and transactions

Use one writer queue as the primary database concurrency mechanism. Each logical
state transition runs as one queued job, reading current database state, checking
conditions, and updating core and backend tables in one transaction.

The feed server owns the connection and transaction lifecycle. The backend can
execute SQLite SQL for its own tables through the supplied connection; it must
not commit, roll back, or open another write connection. Core tables are modified
through core operations using that same connection. Register backend schema
migrations with database initialization. This intentionally makes backend
persistence SQLite-specific rather than introducing a storage abstraction.

Cross-table atomicity does not require foreign-key enforcement. If foreign keys
are disabled, explicit lifecycle cleanup must prevent orphaned backend rows.
Separate queued jobs are separate commits; related writes must share a job.

Retain WAL mode. Use explicit `BEGIN` (deferred) for read/check/write jobs so reads
are included in the transaction. `BEGIN IMMEDIATE` is not required when all writes
use the single writer. The current Python implicit transaction starts at the
first DML write, so entering `_read_write` alone does not include preceding reads.
WAL readers on separate connections can continue reading committed snapshots.

Perform external network calls and filesystem reads before entering the database
transaction. Their results can become stale; validate those cases when applying
them. The queue serializes database state transitions, not the external source.

### Linux file handles

For supported Linux filesystems, use opaque file handles from
`name_to_handle_at()` as object identity evidence and use `open_by_handle_at()`
where supported and permitted. Qualify handles by persistent filesystem identity
and store their type and bytes without interpreting their encoding.

Do not persist a mount ID as filesystem identity. Probe filesystem capabilities;
opening by handle requires `CAP_DAC_READ_SEARCH`. A stale handle triggers
validation/reconciliation, not unconditional deletion. Handle persistence and
rename behavior must be verified for the supported filesystem and handle flags.

The mirror remains a tree: one node has one name and parent. Represent hard-link
entries as separate public nodes, with a potentially shared backend object handle.
Do not make the handle column unique across those entry bindings. Preserving an
entry ID through an ambiguous hard-link rename is not guaranteed.

Handles do not replace entry locators: rename and unlink still require a parent
directory and name. Safe resolution and mutation race handling belong to the
filesystem adapter. Cross-filesystem moves are removal plus creation unless the
backend explicitly implements a stronger identity policy.

## Backend contracts

Establish two internal interfaces before extracting packages.

### Operations

Operations accept public stable IDs and semantic inputs. They return normalized
observations or common errors. Handlers must not construct native references or
catch Synology exceptions.

Keep infrastructure dependencies keyword-only, following repository conventions.
Exact method signatures will be derived from existing handlers and the filesystem
prototype rather than fixed in this document.

Upload bookkeeping belongs to the server, but the backend controls staging and
finalization. A filesystem backend may require a temporary file on the destination
filesystem for atomic finalization. Synology conflict handling remains inside its
adapter.

### Synchronization

Separate notification hints from authoritative observations:

- Invalidation: a node or scope needs to be read again.
- Observation: normalized metadata from a backend read, or a confirmed absence.
- Coverage: which directory listings completed and which scopes remain unknown.
- Progress: optional backend state that can safely be persisted after the work it
  covers is durable.

A backend need not provide notifications, a replayable event stream, ordered source
revisions, or a checkpoint. Polling and full traversal are valid implementations.

Do not model all backends as a Synology scan with renamed `max_id` fields.

## Reconciliation and concurrency

Use one reconciliation mechanism for startup, backfill, and scoped repair.
Conceptually, a session begins for a scope, accepts observations and coverage,
then commits eligible removals and safe progress.

Required invariants:

1. Missing from an incomplete listing does not mean deleted. Failed or inaccessible
   scopes preserve existing nodes and report degraded coverage.
2. A successful listing does not imply an atomic snapshot of the whole tree.
3. Listing-derived removals are conditional on the expected parent/binding and
   reconciliation state. A move observed elsewhere must not be deleted by an
   older listing of its former parent.
4. Scan results must not overwrite newer API or notification-driven reconciliation.
   Coordinate backend reads and application, track invalidations during reads,
   and validate/retry conflicting results.
5. A sequence number assigned when a read starts is not proof of source freshness.
   Likewise, a serialized SQLite writer does not serialize observations.
6. Derived metadata applies only while its source observation remains current.
   Stale enrichment must not resurrect a removed node.
7. Backfill and scoped repair cannot bypass concurrency control through a second
   process. Scoped repair runs inside the server; backfill, `gc`, and `squash`
   are offline maintenance commands that run only while the server is stopped
   (see Observation ordering).
8. Confirm the configured source is present before declaring its root covered.
   An unmounted filesystem exposing an empty mount-point directory must not erase
   the mirror.

The single writer and transaction-local checks solve database check-then-write
races. Add targeted observation validation where external reads can become stale,
rather than a general locking or version framework around every operation.

Prefer a bounded, understandable reconciliation implementation over a universal
version system. Native source revisions can help where their ordering guarantees
are established, but are not mandatory in the common contract.

### Observation ordering

Decided: order mirror writes by when the backend read began, not by when its
result reaches the writer.

- Every backend read takes a monotonic in-process sequence number when it starts:
  a directory listing (startup scan, backfill, moved-directory scan, repair), a
  single-node fetch (webhook, API handler), or a notification-reported removal.
- An upsert applies only if no observation from a later-started read has already
  been applied to that node.
- A removal inferred from a listing's absence applies only if no later-started
  read has touched the node since.
- Sequence numbers older than the oldest in-flight read can be forgotten, so the
  bookkeeping stays bounded.

Rationale: a notification is delivered after the change it reports, so the read it
triggers starts after that change and observes it, while any overlapping listing
that started earlier may be stale. A change landing during that read produces
another notification and another, later read. Correctness therefore relies on
notifications not being silently lost; backends that can lose them need periodic
repair (see Discovery progress).

This replaces the stage 1 acceptance-order claims, which are the special case of
ordering by when a write was accepted. It also lets notifications be processed
during the startup scan instead of after it.

The ordering lives in one process, so only one process may write the mirror at
a time. Startup scan, webhooks, API writes, and scoped repair run inside the
server. Backfill, `gc`, and `squash` stay offline maintenance commands that run
only while the server is stopped; backfill shares the reconciliation semantics
but not the server process. Startup scan already reconciles the whole mirror, so
backfill is a targeted offline repair, not something run against a live server.

## Discovery progress and public cursors

Public feed cursors refer only to committed server changes. Backend checkpoints
refer only to backend discovery. Scan coverage is separate from both.

For Synology:

- Keep `sync_id`, `max_id`, pre-scan watermarks, count checks, and pruning inside
  the adapter.
- Persist progress only after every observation it covers is durable.
- Incomplete coverage must either prevent checkpoint advancement or retain durable
  repair work that cannot be skipped by the new checkpoint. Initially prefer
  withholding advancement for the affected mount.
- Do not treat matching child counts as proof that metadata is unchanged.

For filesystems:

- Do not assume a durable inotify/fanotify cursor.
- Establish watches and scan with a race-aware strategy, then reconcile scopes
  invalidated during setup and traversal.
- Queue overflow or lost coverage schedules repair. Restart recovery normally
  requires a traversal unless another trustworthy recovery mechanism is available.
- Timestamp or hash caches are backend optimizations, not proof of subtree coverage.

Backends with potentially silent event loss need periodic repair. The adapter
defines the necessary repair scope; the server can supply scheduling facilities.

Change-log compaction must expose a retention boundary or epoch so old public
cursors fail explicitly and clients can resynchronize.

## Persistence, hashing, and enrichment

Persist basic observations before optional media probing. Media probing failure
must not discard discovered nodes or permit unsafe checkpoint advancement.

Separate durable discovery from publication requirements. The independent feed
server can internally persist pending checksums, but every file exposed through
the drive contract must have a complete checksum and a compatible client hasher.
The client does not define verification without a checksum. Do not add silent
verification skipping or algorithm-only file nodes to the drive contract.

### Hash model and client adaptation

Use flat fields rather than a nested hash object:

```python
hash: str
hash_algorithm: str
```

`hash` remains the digest string, preserving existing truth checks and digest
comparisons. `hash_algorithm` is an explicit normalized field, persisted alongside
the digest rather than hidden in raw/private provider metadata. Both are strings;
an empty algorithm means undefined, with no nullable field or separate hash type.

| Drive node | `hash` | `hash_algorithm` |
| --- | --- | --- |
| Directory | Empty string | Empty string |
| File | Complete, nonempty digest | Nonempty algorithm identifier |

Validate these invariants when constructing/adapting drive nodes. Use precise
algorithm identifiers with documented computation and digest encoding, including
distinct identifiers for any provider-specific hashing scheme. Unknown algorithms
must fail explicitly rather than falling back to another algorithm.

The feed payload can use the same flat shape without depending on the drive model.
Define any pending-file representation independently.

Decided: the feed exposes nothing incomplete. A node is published only once its
checksum and media metadata are complete, and completion produces the feed update
that makes it visible. When an already visible file changes and its new checksum
or media metadata is pending, the last complete version stays visible and the
update is withheld until the new metadata is complete, then published as one
change. Clients never observe a partially updated node. During that window a
download may not match the visible digest; verification fails and the client
retries. Removing and re-adding the node was rejected because clients such as
duld would observe a deletion. Synology reports the checksum with the rest of its
metadata, so this window currently arises only from local media probing.

The Google adapter supplies `md5` from its provider contract; the feed adapter
reads the algorithm from the server. Backend-native or server-calculated checksums
are both valid. Hash algorithm selection is per file, without exposing mounts or
putting hashing policy on directory nodes.

The current transfer paths support this design:

- duld and CLI uploads obtain a file node before calculating the local checksum.
- Existing-file checks use the existing file node.
- CLI downloads verify against the source file node after transfer.
- The CLI hash helper already calls `Drive.get_hasher_factory(node)`.

No pre-upload algorithm negotiation is needed for those paths. Core transfer
helpers move bytes; their callers perform checksum verification afterward.

Coordinated changes are required outside this repository:

- `wcpan-drive-client`: add `hash_algorithm` to `Node`; make
  `FileService.get_hasher_factory(node)` accept file context. Single-source Drive
  forwards it; multi-source Drive forwards the unscoped node to its service.
- Provider clients: populate the algorithm and select a compatible per-file
  factory. Google can keep its fixed MD5 implementation; the feed client selects
  from the returned node rather than hard-coding MD4.
- `wcpan-drive-client-sqlite`: persist the algorithm; existing snapshots are rebuilt or
  migrated manually. Remove assumptions that hash nullability identifies a
  directory; pending or malformed file metadata must not change node kind.
- `wcpan-drive-client-crypt`: forward node context and preserve the encrypt-before-hash
  wrapper. A global algorithm lookup must not bypass service transformations.
- CLI/duld: preserve digest comparisons and required-checksum verification, update
  fixtures/contracts, and exercise mixed algorithms within one feed service.
- CLI process-pool hashing: factories must remain picklable, using suitable
  module-level callables or partials rather than closures capturing an HTTP client.

Hashing a changing local file requires validation and retry; a close-write event
is a scheduling hint, not a guarantee that content will remain unchanged. Hash and
media updates must be conditional on the source state they examined.

Keep SQLite execution separate from backend blocking I/O and bulk content reads.
The filesystem backend owns bounded pools. A timeout does not cancel an underlying
blocked thread, so pool saturation and shutdown need explicit behavior.

## Compatibility and configuration

Use separate server settings, named backend instances, and virtual mount names.
The following is a proposed schema, not an implemented configuration change:

```yaml
version: 3

server:
  host: "0.0.0.0"
  port: 8080
  database_url: "sqlite:////data/feed.db"
  upload_tmp_dir: "/data/uploads"

backends:
  nas:
    type: synology
    url: "https://nas.example.com:5001"
    username: "feed-user"
    password: "..."
    webhook:
      callback_url: "https://feed.example.com"
      app_id: "wcpan-drive-server-synology"
    local_paths:
      "/volume1/photos": "/mnt/photos"

  disk:
    type: filesystem
    watch: auto

mounts:
  photos:
    backend: nas
    source: "/volume1/photos"
  documents:
    backend: disk
    source: "/srv/documents"
```

The core parses `server`, backend instance names and types, and mount names and
backend references. Each backend parses its own settings and mount `source`.
Filesystem settings such as `watch: auto` remain illustrative until that backend
is implemented. Multiple mounts may share one backend instance.

Mount keys are single-component virtual names: `photos` exposes `/photos`.
They are not arbitrary virtual paths; nested mount names are outside the initial
scope. Reject empty names, names containing `/`, and `.` or `..`. `/` is the
server's virtual root. Backend name rules for source entries remain separate.

Keep `callback_url` under the Synology webhook configuration. Its only current
operational use is webhook registration; the generic server has no `public_url`
requirement. A filesystem-only deployment does not need a callback URL.

Backend instance names identify persistent source instances. Changes to a mount's
backend or source require an explicit reconfiguration policy and checkpoint
invalidation. Locators, handles, `max_id`, and other discovery progress belong in
the database, never in configuration. Version 3 is proposed as the successor to
the existing version 2 schema.

- Preserve existing client imports, HTTP routes, error behavior, and CLI behavior
  during the internal refactor unless a separately reviewed migration requires change.
- Release a client decoder that tolerates absent `mutable_id` before removing the
  field from server responses. Keep a compatibility field during transition.
- Keep Synology credentials, native mount paths, and webhook settings in backend
  configuration; generic server settings remain in the server configuration.
- Provide explicit database migration and rollback/backup guidance before extraction.
  Existing node IDs and public feed continuity should survive migration.
- Update `README.md`, `server.example.yaml`, and configuration tests together when
  installation, commands, or configuration behavior changes.

## Implementation stages

### 1. Establish correctness baselines

Existing scan tests already cover failed listings preserving subtrees, failed
mounts withholding `max_id` advancement, cross-mount reparenting, and count-based
pruning. Add the remaining race and staleness coverage: scan results overwriting
newer webhook or API-driven state, an older listing of a former parent deleting a
moved node, and stale media enrichment updating or resurrecting a node. Verify the
suspected failure paths before treating review findings as established bugs. Fix
confirmed issues within the current package.

Deliverable: documented invariants supported by regression tests.

Status: done. `packages/server-synology/tests/test_server_races.py` reproduced and now guards these:

- Enrichment that was queued or still running (metadata queue, inline `upsert`)
  overwrote a newer upsert, resurrected a deleted node, or recreated a child of
  a deleted parent. `NodeSyncService` now claims a node when it accepts an
  observation; a write applies only while its claim is the latest one, and
  `apply_observations` skips records whose parent is gone in the same
  transaction.
- Backfill deleted a node that a webhook had moved out of the listed folder.
  Listing-derived removal is now conditional on the node's current parent
  (`remove_listed_child`; replaced in stage 3 by end-of-pass removals).

Verified as not reproducible in the current design: startup scan results cannot
overwrite webhook or API state, because webhooks are processed and API writes
accepted only after the scan and its enrichment drain.

Known remaining gaps, deferred to stage 3:

- A node moved during the startup scan, after its new parent was listed and
  before its old parent was, is removed at the end of the scan. The queued
  webhook recreates it, so the feed shows removal and creation rather than a move.
- Backfill runs in a separate process. Its stale upserts could revert a
  concurrent webhook update. Claims are in-memory per process; only the
  conditional removal and the parent check protect across processes. Resolved
  by running backfill only while the server is stopped (see Observation
  ordering).

### 2. Separate normalized records and backend state

Introduce internal protocol/mirror types, Synology locator persistence, and a
transaction-bound application path (server schema 5). Stop counting locator changes as public
metadata updates. Prepare tolerant client decoding and migration tests.

Deliverable: generic records contain no Synology locators; existing clients remain
compatible with server responses.

Status: done. `NodeRecord` no longer has `mutable_id`. The server passes
`NodeObservation` (record plus Synology locator) through `NodeSyncService`, and
`apply_observations` commits a node and its `synology_locators` row in one
transaction. A change row is emitted only when the public node row changes, so
locator-only updates are silent. Bindings are deleted with their subtree and by
dangling-node GC. Schema 5 migrates existing locators and preserves node IDs and
the change feed (`packages/server-synology/tests/test_server_mount_max_ids.py`). The protocol decoder
tolerates an absent `mutable_id`; the encoder still emits it as an empty
placeholder until old decoders are retired.

Deferred: source-instance scoping of locators and checkpoints needs backend
instance names (configuration version 3), so it moves to stage 3. The existing
`mounts` table already serves as the Synology checkpoint table, keyed by mount
name and invalidated when the source path changes.

### 3. Introduce operations and reconciliation boundaries

First, replace the stage 1 claims with observation ordering, applied to scan,
webhook, API, and backfill writes and to listing-derived removals. Cover the
stage 1 known gaps: a node moved during the scan is not removed, and backfill
cannot revert a newer webhook update (backfill runs only while the server is
stopped).

Status: observation ordering is implemented (`services/ordering.py`); every
`NodeSyncService` write takes the `ReadSeq` of the read it came from. Webhooks
are still processed after the startup scan, so ordering alone cannot save a node
that moved during the scan; instead, each end-of-scan removal root is first
fetched by permanent link. If it still exists under a known parent, the move is
recorded instead of the removal. Only a network failure preserves the node; API
errors fall back to removal, so the check never removes more than before.
Backfill, `gc`, and `squash` remain offline commands that run only while the
server is stopped, so no internal maintenance endpoint is needed.

Move native API calls and exception handling out of generic handlers.

Status: the node handlers (create, rename/move, delete, download) call a
backend-neutral `NodeOperations` service (`services/operations.py`) and map its
`OperationConflictError` and `OperationUnavailableError` to HTTP responses. The
Synology implementation, including conflict recovery and the ordered mirror
writes, is `SynologyNodeOperations` in the adapter. Upload sessions stay generic;
the transfer and conflict lookup go through an `Uploader`.

Route scan,
backfill, and scoped repair through common reconciliation semantics.

Status: startup scan and backfill share `ReconcileSession`
(`services/reconcile.py`). Listings report the nodes they saw and the folders
they could not cover; removals are decided once at the end of the pass, skip
nodes touched by newer reads, and fetch each candidate subtree root directly
(a backend `Locate` function) before removing it. Backfill therefore no longer
deletes a node that moved between two of its listings, and `remove_listed_child`
is gone. Change detection stays per caller: startup scan uses `sync_id`
checkpoints, backfill compares every field. Scoped repair will open the same
session.

Separate
durable discovery from optional enrichment, following the publication rule in
Hash model and client adaptation.

Status: observations are still published only after enrichment, so the feed
never shows an incomplete node and a changed file keeps its last complete
version. A failed media probe is no longer dropped: the node is recorded in
`pending_enrichment` (schema 6) unless a later read already observed it. Any
applied observation or removal of the node clears the entry, and startup
re-observes each pending node after the scan (`retry_pending_enrichment`),
removing it if the backend no longer has it. Checkpoints may therefore advance
past a failed update without losing it. A probe that keeps failing leaves the
file withheld; a file with no reachable local path is published without probed
dimensions, as before.

Then move Synology orchestration (configuration, mount resolution, scan,
webhook, backfill, operations, and the server command) into
`wcpan-drive-server-synology`, and remove the `wcpan-synology` dependency from
`wcpan-drive-server`. The generic server must not import the adapter package.

Deliverable: generic HTTP and mirror modules import neither `wcpan.synology` nor
`wcpan.drive.client` types. Core and backend persistence share queued transactions.
The Synology adapter lives only in its own distribution.

Status: done. `wcpan-drive-server` no longer depends on `wcpan-synology`, and
`packages/server/tests/test_server_boundaries.py` rejects backend or client imports in it. The
generic app takes a `Backend` (`backend.py`): its `BackendSchema` hooks run on
the core's connection inside core transactions (create tables, bind a locator,
forget locators of removed nodes), its routes are added to the app, and its
runtime supplies `NodeOperations`, an `Uploader`, initial discovery before ready,
and ongoing background work. `NodeObservation.locator` is an opaque
`BackendLocator`; backend queries and checkpoint writes use `StorageService.read`
and `write`, the latter through `NodeSyncService.enqueue` so checkpoints follow
the mirror writes they cover. Configuration keeps generic fields (database,
listener, mounts as label to source path, `local_paths`) in the core and leaves
the `synology` section to the adapter; the file format is unchanged. The adapter
owns `synology_locators` and the `mounts` checkpoint table; the historical 4→5
core migration still names `synology_locators` because it moved existing data.
`wcpan.drive.server` is now `wcpan.drive.server.synology.main`.

### 4. Validate with a minimal filesystem backend

Start with traversal and read operations on a documented supported filesystem,
then add watcher invalidations and recovery. Exercise stable handle identity,
hard links, rename, replacement, inaccessible directories, source disappearance,
restart, overflow recovery, and reads finishing out of order. Add mutations and
upload staging to verify that those interfaces are also backend-neutral.

Contract changes found here are made across the server and Synology packages in
this workspace; the extraction in stage 3 does not freeze the backend interface.

Deliverable: both backends satisfy the common contract without filesystem code
pretending to provide a Synology checkpoint or immutable entry locator.

### 5. Complete distribution boundaries

The workspace initialization establishes independently buildable distributions,
and the hash model and service interface are coordinated across the client
framework, SQLite, providers, crypt, and CLI. Remaining work: migrate duld, keep
client and server dependency sets separate, and validate independent wheel
installations, not only imports within the shared development environment. No
compatibility wrapper is planned for the retired Synology client package.

## Validation

Use `unittest` and the existing async aiohttp patterns. Run `make test` and
`make lint` for implementation changes. Add focused tests for:

- Locator updates without public changes and atomic node/binding persistence.
- Crash recovery around observation writes and checkpoint advancement.
- Partial coverage, permission failures, and missing sources without false removals.
- Moves between directories, stale reads, stale enrichment, and tombstones.
- Existing/new client and server wire compatibility.
- Flat hash field validation, checksum completion visibility, and mixed-algorithm
  upload/download verification through core, CLI, and duld.
- Algorithm persistence across cache reloads, crypt hasher wrapping, and factory
  serialization through the CLI process pool.
- Filesystem hard-link entry semantics, handle staleness, restart, and watcher loss.
- Upload staging/finalization and backend error translation.
- Cursor expiration after compaction and maintenance writer exclusion.

Tests should verify behavioral guarantees rather than mirror implementation details.

## Decisions still requiring review

1. Finalize filesystem backend scope before adding its workspace member.
2. Confirm separate public nodes for hard-link entries and limited continuity for
   ambiguous entry renames.
3. Choose the first supported filesystem/deployment requirements, including whether
   handle-based reopening privileges are required or optional.
4. Choose release automation and independent versus coordinated version policy
   after the initial `0.1.0` releases.

## References

This plan incorporates two Claude architecture reviews. Review suggestions are
design inputs; suspected current bugs still need focused reproduction.

- Existing implementation: `packages/protocol/src/wcpan/drive/protocol/types.py`
  and `packages/server/src/wcpan/drive/server/{services,handlers,app.py}`.
- [Linux file handles](https://man7.org/linux/man-pages/man2/open_by_handle_at.2.html)
- [inotify behavior and limitations](https://man7.org/linux/man-pages/man7/inotify.7.html)
- [fanotify behavior and limitations](https://man7.org/linux/man-pages/man7/fanotify.7.html)
- [Inode and hard-link semantics](https://man7.org/linux/man-pages/man7/inode.7.html)
