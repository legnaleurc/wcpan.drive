# Drive client/server workspace and extraction plan

Status: the uv workspace and namespace migration are initialized at version
`0.1.0`. The flat `hash`/`hash_algorithm` model and per-node hasher selection are
implemented in this repository (protocol, server, and all client packages; client
service API version 6). Stages 1 to 4 are complete: the generic server, the
Synology and filesystem backends, and config version 3. Stage 5 waits only on
publishing the `0.1.0` distributions; see its status.

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
| `wcpan-drive-client-feed` | `wcpan.drive.client.feed` | HTTP server provider and client-model adaptation |
| `wcpan-drive-client-crypt` | `wcpan.drive.client.crypt` | Encryption middleware |
| `wcpan-drive-client-sqlite` | `wcpan.drive.client.sqlite` | Client metadata cache |
| `wcpan-drive-client-config` | `wcpan.drive.client.config` | YAML drive configuration loader |
| `wcpan-drive-client-media` | `wcpan.drive.client.media` | Local MIME type and media probing |
| `wcpan-drive-client-cli` | `wcpan.drive.client.cli` | Client command line; no public library API |
| `wcpan-drive-plugin` | `wcpan.drive.plugin` | Plugin discovery and factory settings (check, scaffold) for client and server |
| `wcpan-drive-protocol` | `wcpan.drive.protocol` | Lightweight HTTP wire models and serialization |
| `wcpan-drive-server` | `wcpan.drive.server` | Mirror, HTTP server, reconciliation, and backend extension contracts |
| `wcpan-drive-server-synology` | `wcpan.drive.server.synology` | Synology adapter |
| Future `wcpan-drive-server-filesystem` | `wcpan.drive.server.filesystem` | Filesystem backend; not yet shipped |

Client and server roots are namespace packages with explicit public modules and
private implementation. Neither needs a separate core distribution initially;
extract contracts only if dependency or release needs justify it. The protocol
package depends on neither runtime. The feed provider depends on the client
framework and protocol, never on the server. The server must eventually depend
on neither the client framework nor concrete backends. The current initialization
removes client-framework imports from the server but still has Synology-specific
orchestration; the adapter distribution supplies the runnable server command.

Breaking migration is accepted: retire old distribution/import names directly,
without compatibility wrappers. Newly named distributions begin at `0.1.0`.
Existing external consumers, including duld, require coordinated migration.

### Client helper packages

Status: implemented in this repository; external consumers still need to
migrate (see stage 5). `wcpan.drive.client.cli.lib` was used as a utility
library by duld, dvd, and drive.py, which therefore installed the CLI and its
interactive dependencies. It is split by responsibility:

- `wcpan-drive-client-config` owns the drive configuration (currently version 4):
  YAML loading, schema validation before any factory import, dotted-path factory
  resolution, and `create_drive_from_config(path)`. Core keeps its typed
  `SourceConfig` API and does not adopt the schema as a public contract.
  Configuration files are trusted executable wiring; `safe_load` does not make
  factory imports safe.
- `wcpan-drive-client-media` owns `get_mime_type` and the image/video/media info
  helpers (python-magic, pymediainfo). Declared dependencies do not guarantee the
  native libraries at runtime. The server keeps its own probing; the server
  boundary forbids client imports.
- Core `client.lib` gains `get_file_hash`, a client operation over
  `Drive.get_hasher_factory(node)`. Execution stays caller-owned; document that
  process pools require a picklable hasher factory.
- `create_executor` stays application wiring (CLI, duld). Its worker SIGINT
  handling is application policy and currently applies only to the `spawn`
  start method, so it is not promoted to a public API.
- `client-cli` keeps commands, the shell, queueing, and output helpers, and
  depends on `-config` and `-media` (and PyYAML for output). `cli.lib` is removed
  without re-exports; consumers migrate directly.
- Core `create_multi_drive` must reject duplicate source names, which currently
  overwrite each other.

New helpers go to the package that owns their responsibility. A new package is
justified when consumers need to install a responsibility independently;
dependency weight is a secondary signal. Stdlib-only does not by itself mean core.

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
- The hash helper (`get_file_hash`, moving to core `client.lib`) already calls
  `Drive.get_hasher_factory(node)`.

No pre-upload algorithm negotiation is needed for those paths. Core transfer
helpers move bytes; their callers perform checksum verification afterward.

Coordinated changes are required outside this repository:

- `wcpan-drive-client`: add `hash_algorithm` to `Node`; make
  `Backend.get_hasher_factory(node)` accept file context. Single-source Drive
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
Implemented as configuration version 3 (see status below); `type` names the
backend factory, for example `wcpan.drive.server.synology.create_backend`:

```yaml
version: 3

server:
  host: "0.0.0.0"
  port: 8080
  database_url: "sqlite:////data/feed.db"
  upload_tmp_dir: "/data/uploads"

backends:
  nas:
    type: wcpan.drive.server.synology
    url: "https://nas.example.com:5001"
    username: "feed-user"
    password: "..."
    webhook:
      callback_url: "https://feed.example.com"
      app_id: "wcpan-drive-server-synology"
    local_paths:
      "/volume1/photos": "/mnt/photos"

  disk:
    type: wcpan.drive.server.filesystem
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
the database, never in configuration. Version 3 succeeds the version 2 schema.

Status: implemented. `wcpan-drive-server` owns the `wcpan.drive.server` command.
Each instance's `type` is the import path of an async context manager factory,
like the client's service factories, called as `factory(context, **settings)`.
The context carries the instance name, its mounts, the core services, a route
registry, and an `offline` flag. The factory registers its schema (the core
creates its tables and drops rows orphaned by an offline `gc`), registers its
routes, and connects when serving; offline it only parses settings. Routes are
served under one core route, `/api/v1/backends/<name>/<path>`, because the
router is frozen before factories run. Maintenance commands are addressed as
`backend NAME ARGS...`: the core passes the arguments to the instance's
`command`, which parses them itself and connects only once they parse, so
`--help` never reaches the source. Backend packages ship no scripts, so
`uvx --with=wcpan-drive-server-synology --with=wcpan-drive-server-filesystem
wcpan.drive.server serve` runs both. Version 2 files load as one Synology
instance named `synology`.

The core now creates the root and mount nodes (publishing mounts added later),
routes operations and uploads to the backend serving a node's mount
(`BackendRouter`; moves between backends are refused), starts every instance,
and becomes ready once all have finished initial discovery. Each instance writes
through `NodeSyncService.for_backend(name)`, which tags observations so storage
binds locators with that instance's `BackendSchema`; pending enrichment records
its instance (server schema 4) so each backend retries only its own. `local_paths`
moved into backend settings; the core asks the backend for each mount's host
paths. Synology webhooks use the per-instance route
`/api/v1/backends/<name>/webhook`.

Not done: an explicit policy when a mount changes backend or source beyond the
existing checkpoint and filesystem-identity checks; node IDs rely on each
backend minting globally unique IDs rather than an enforced namespace (see
Core-minted node identity).

- Preserve existing client imports, HTTP routes, error behavior, and CLI behavior
  during the internal refactor unless a separately reviewed migration requires change.
- `mutable_id` is removed from server responses in the same `0.1.0` release
  as the client decoder that tolerates its absence; older clients must upgrade.
- Keep Synology credentials, native mount paths, and webhook settings in backend
  configuration; generic server settings remain in the server configuration.
- Provide explicit database migration and rollback/backup guidance before extraction.
  Existing node IDs and public feed continuity should survive migration.
- Update `README.md`, config scaffolds, and configuration tests together when
  installation, commands, or configuration behavior changes.

## Core-minted node identity

Status: proposed. Reviewed by Codex as a per-backend database split, then
narrowed to public node IDs minted by the core in one database, and reviewed
again. Rebuilding the server database and client caches is accepted, so this
design has no in-place migration. The file split is deferred (see Deferred:
per-backend database files). This amends "Backend state lives in the same
SQLite database as the mirror" (Node identity and backend state) once
implemented, and replaces backends minting globally unique IDs.

### Goal

Backends today mint the public node ID themselves, and neither those IDs nor
backend state are unique across instances by construction. The filesystem
backend mints random UUIDs (`_scan.py` `_new_id`), but Synology uses the NAS
permanent link, so two Synology instances can collide. Backend state already
collides between two instances of the same type, before `nodes` is involved:

- `synology_locators` is looked up by `file_id`, so a webhook for one NAS can
  resolve to the other NAS's node.
- `fs_entries` is looked up by `identity`, which two filesystem instances on the
  same host filesystem can share.

The README only says node ID collisions "are not expected".

Two changes fix this separately:

- Public node IDs are minted by the core and mapped to `(backend, native ID)`,
  so they are unique by construction and carry no backend spelling.
- Each backend instance gets its own tables for backend-owned state, so its
  lookups cannot see another instance's rows.

The HTTP feed stays one stream with one cursor and one total order of changes.

### Why one database is enough

No operation writes to two backends: `BackendRouter.update` refuses moves
between backends (`services/routing.py`), and uploads, creates, and deletes are
routed to one backend. Isolation therefore needs an ID map and separate backend
tables, not separate files. One file keeps one `change_id` sequence (a simple
cursor and a total order), and the node upsert, change row, and locator binding
in one transaction.

### Identity map

The core owns `node_ids(id PRIMARY KEY, backend, native, UNIQUE(backend,
native))`. The root (`_`) and mount IDs are core IDs without map rows.

ID spaces: every ID a backend exchanges with the core is public, in
observations (`id` and `parent_id`), deletions, mirror reads, ordering, and
returned records. Native IDs appear only between a backend and its source.
Backends translate where they already convert source data, so the core
contract, `NodeSyncService`, `ObservationOrder`, the enricher, uploads, and the
HTTP layer keep working in one ID space.

The core gives each backend instance, bound to that instance:

- `public(natives) -> ids`: lookup or mint. Hits are read from a read
  connection; misses run one writer job that does `INSERT OR IGNORE` then
  reads back, so concurrent requests for one native ID get one public ID.
  Minting does not need the node's transaction: a map row without a node is
  harmless, and `gc` prunes it.
- `lookup(native) -> id | None`: lookup only, for deletions and other events
  that must not mint.
- `native_of(id) -> native`: for calls to the source.
- `mint() -> id`: a fresh ID whose native ID is itself, for backends that keep
  their own identity mapping.

Mappings never change once written, so lookups can be cached in memory.
`public` must not be called from inside a writer job (it waits on the writer).

Map rows are kept when their node is deleted, so a delayed observation of a
deleted native ID gets the same public ID, and `ObservationOrder`, keyed by
public ID, still rejects it. Withheld nodes, upload-conflict records that are
never mirrored, and nodes under failed listings all have stable IDs for the
same reason. Only offline `gc` prunes map rows that have no node and no pending
enrichment.

Backend conversion sites:

- Synology: converting API info to a record calls `public(permanent_link)`;
  parents keep resolving to public IDs through locators or mount roots
  (`_paths.py` `resolve_parent`). Sites that send a node ID to the NAS
  (`_operations.py` `_stable_node_ref`, `_paths.py` `locate`) call `native_of`.
  Webhook deletions call `lookup`; an unknown permanent link has nothing to
  delete. Comparisons that already exist inside the backend, such as the move
  check `updated.id != record.id` (`_operations.py`) and backfill's comparison
  with mirror IDs (`_backfill.py`), stay correct because both sides are public.
- Filesystem: `_new_id` calls `mint`; `fs_entries` keeps mapping identity to
  the node ID. Scan matching, `claimed`, and traversal already use those IDs.

Routing: the root is rejected; a mount ID routes to the mount's owner, as
create, upload, and move destinations are usually mounts; any other ID routes
by its map row's backend, which also covers withheld nodes. This replaces the
ancestor walk in `backend_of`. An ID without a map row is not found; a
registered backend that is not running gives 503.

Independent client fixes:

- `client-feed` percent-encodes every ID it puts in a URL (`quote(id,
  safe="")`; aiohttp decodes `match_info`). It interpolates them raw today
  (`feed/service.py`, `readable.py`, `writable.py`).
- Client source names must not contain `:`; `client-config` accepts any
  non-empty name today, which already breaks `client/_drive.py` `_parse_id`.

The README note "collisions are not expected" becomes a guarantee.

### Backend tables

Per backend instance, the backend-owned tables (`BackendSchema`: locators,
entries, checkpoints) and their indexes are created under names derived from
the instance's registry key. The registry records, per instance: name, type,
key (an opaque `[a-z0-9]` token assigned the first time the instance is seen,
as mount IDs are), and backend schema version. Renaming an instance in the
config makes it a new backend. SQL object names are built only from registry
keys, never from config strings, so interpolating them is safe.

- `BackendSchema.ensure_schema` and the hooks (`bind_locator`, `forget_nodes`,
  `forget_orphans`) receive the instance's namespace; backend stores are
  already built per instance, so they hold it.
- Backend rows stay keyed by public node ID. Since public IDs are unique, the
  backend joins to `nodes` (`_get_node_by_locator`, `_nodes_by_identity`) and
  orphan cleanup (`NOT IN (SELECT id FROM nodes)`) need no instance filter.
- Lookups by `file_id` and `identity` read only the instance's own table.
- Backend schema versions are per instance, checked at startup like the core
  `user_version`; a mismatch requires a rebuild of that instance.

Per-instance tables were chosen over an instance column on shared tables: with
a column, every backend key, index, and query must carry the instance, and a
missed filter silently reads another instance's rows.

### Cursor

The cursor stays an opaque string for clients (`client-feed` already treats it
so; only `handlers/changes.py` parses it as an integer). It is the database
epoch plus the global `change_id`.

- `"0"` (what `client-feed` returns as its initial cursor) is a bootstrap
  token: everything from the start.
- A cursor is stale when its epoch differs from the database's, or its
  `change_id` is beyond the current maximum (an older database restored under
  the same epoch). Stale cursors get 410 Gone. Legacy integer cursors other
  than `"0"` are stale.
- `GET /cursor` returns the current position.

### Cursor reset

Independent of the identity map, and already missing today: `squash` warns that
it resets consumer cursors, but nothing tells consumers, and old integer
cursors keep working while missing dropped tombstones.

- Server: the database stores an epoch (random, set when the database is
  created and changed by `squash` and restore). 410 for stale cursors.
- `client-feed`: map 410 to a dedicated exception.
- Client cache: a transactional reset operation for one source (the cache has
  none today).
- `_Source.sync`: catch the exception while holding its lock, reset that
  source's cache, refetch the root, and restart from the initial cursor. Retry
  at most once per sync call. `ChangeAction` has no reset event, so before
  clearing, the reset yields a removal for every node cached for that source,
  then the full replay; downstream consumers stay correct without a new change
  type. The failure must not escape into the multi-source `TaskGroup` and cancel
  sibling sources.

### Lifecycle

These run offline (server stopped), so no upload session, scan, or enrichment
job is in flight. None needs a cursor reset: clients do not cascade a removal
to descendants (`inner_delete_node_by_id` only unlinks the node), so every
removal is one tombstone per node, which the change log expresses directly.

- `backend NAME drop`: write one tombstone per node of the instance, delete its
  nodes, pending enrichment, and map rows, drop its tables, and remove its
  registry entry, in one transaction.
- `backend NAME rebuild`: the same tombstones and deletions, keeping the map
  rows and registry entry and emptying its tables. Initial discovery runs on
  next start; items whose native ID is stable (Synology permanent links) come
  back under their old public IDs. Map rows left unused are pruned by a later
  `gc`, so a rebuild that does not finish discovery before `gc` loses only ID
  continuity, not data.
- Changing a mount's backend: the old backend's subtree under that mount is
  deleted with tombstones (its `gc` sees the mount is no longer its own), and
  the new backend discovers it with new public IDs. The mount node and ID stay.
- A backend removed from the config but still registered is reported at startup
  and kept until dropped, so a config typo does not discard a mirror. Its nodes
  stay in the mirror; content reads and writes on them fail (503), since no
  backend serves them.

Readiness stays global for now: one failed initial discovery keeps the whole
server unready (`app.py`). Per-backend readiness is a later option.

### Maintenance

- `gc` runs per backend: reachability starts from that backend's mount IDs over
  the nodes its map rows own, and nodes under a mount no longer served by it are
  deleted with tombstones. It then prunes map rows with no node and no pending
  enrichment.
- `squash` stays global and changes the epoch.

### Database rebuild

There is no in-place migration. The new schema version refuses to open an older
database and tells the operator to move it aside; the server then creates a new
database with a new epoch, runs initial discovery, and mints new public IDs.
Clients reset through stage 1 (their integer cursors are stale). Existing
consumers that persist node IDs (duld) must be checked. This also clears any
state where two Synology instances already collide.

### Staging

1. Cursor reset: epoch, 410 on stale cursors, `"0"` bootstrap, client cache
   reset and resynchronization. Useful alone, since `squash` needs it today.
   Also percent-encode IDs in `client-feed` URLs and reject `:` in client
   source names. Implemented: the epoch is a `metadata` row (added to existing
   databases on open, no schema version change), the cursor is
   `<epoch>.<change_id>` and is parsed only in `handlers/changes.py`, and the
   client raises `StaleCursorError` on 410.
2. Identity and isolation, in one release with the database rebuild: the
   registry, `node_ids` with `public`, `lookup`, `native_of`, and `mint`,
   Synology and filesystem conversion sites, routing by mount owner and map
   row, per-instance backend tables and indexes with the namespace passed to
   `BackendSchema`, per-instance schema versions, and per-backend `gc`. Shipping
   identity without table isolation would leave the `file_id` and `identity`
   collisions in place.
3. Lifecycle commands: `drop`, `rebuild`, and mount reassignment.

Not part of this work: a separate `NodeSyncService` per backend. Today the
`for_backend` views share the write and metadata queues, `ObservationOrder`,
and the enricher (`services/sync.py`), so one backend's enrichment backlog can
delay another. Separating them is a scheduling improvement, not needed for
identity, and can follow later.

Tests: bootstrap and stale cursors (epoch, restored database, legacy integer
cursors); client reset without cancelling sibling sources; the same native ID,
Synology `file_id`, and filesystem `identity` in two instances of one type;
concurrent `public` calls for one new native ID getting one ID; `lookup` not
minting; a delayed observation after deletion keeping its ID and being rejected
by ordering; Synology operations, `locate`, webhook deletions, and moves through
`native_of` and `lookup`; Synology moves not deleting an unchanged node;
routing to mounts, ordinary nodes, withheld nodes, unknown IDs, and stopped
backends; orphan cleanup in one instance leaving the other's rows; per-instance
schema version mismatch; drop emitting one tombstone per node; rebuild
restoring Synology nodes under their old IDs; `gc` pruning unused map rows;
moves between mounts of one backend.

### Deferred: per-backend database files

Giving each backend its own SQLite file (with a small core database for the
root, mounts, registry, and identity map) was designed and reviewed, then
deferred. The identity map and per-instance tables already give the isolation
the split would, so the split would only add: containment of a corrupt or
oversized file, no shared writer, and restore of one backend from a file copy.

It would cost: a composite cursor (one `(key, epoch, change_id)` component per
file, round-robin pages for fairness, `has_more` over shards a page did not
reach) and the loss of a total order across backends; a structural read facade
so backend maintenance (Synology backfill) can see the root and mounts from
another file; mount parents checked across files; a registry of database paths
that must fail startup when a file is missing; and backups that are not atomic
across files. Attaching backend files to one connection (`ATTACH`) does not
avoid this: in WAL mode a transaction across attached files is atomic per file
only, so a crash could commit a node without its locator.

Revisit it if the single file becomes a measured problem (size, write
contention, or corruption recovery). Public IDs would not change, since minting
does not share the node's transaction; only the cursor format would, which
clients handle through stage 1's reset.

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
transaction-bound application path (server schema 4). Stop counting locator changes as public
metadata updates. Prepare tolerant client decoding and migration tests.

Deliverable: generic records contain no Synology locators; existing clients remain
compatible with server responses.

Status: done. `NodeRecord` no longer has `mutable_id`. The server passes
`NodeObservation` (record plus Synology locator) through `NodeSyncService`, and
`apply_observations` commits a node and its `synology_locators` row in one
transaction. A change row is emitted only when the public node row changes, so
locator-only updates are silent. Bindings are deleted with their subtree and by
dangling-node GC. Schema 4 migrates existing locators and preserves node IDs and
the change feed (`packages/server-synology/tests/test_server_mount_max_ids.py`). The protocol decoder
ignores `mutable_id` from older servers, and the encoder no longer emits it.

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
`pending_enrichment` (server schema 4) unless a later read already observed it. Any
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
`BackendLocator`; backend queries and checkpoint writes use `MirrorStore.read`
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

Decided scope for the first filesystem backend (`wcpan-drive-server-filesystem`):

- Started read-only, with discovery by full traversal at startup plus periodic
  rescans; the watcher and writes followed (see status below). Deletes are
  permanent.
- Each directory entry is its own public node; hard links share a backend handle,
  and an ambiguous hard-link rename may appear as removal plus creation.
- Identity comes from `name_to_handle_at()` qualified by the filesystem ID, with
  ext4 the first supported filesystem. `open_by_handle_at()` is optional, so the
  server runs unprivileged and reads by path, validating identity. Without handle
  support, identity falls back to the inode number.
- One backend per server: the configuration keeps the version 2 shape, and
  multi-backend configuration (version 3) follows once this backend works.
- The server computes checksums for files, since the filesystem has none.

Status: the backend is implemented, first read-only. Discovery runs through
`ReconcileSession` with the same ordering and confirmed removals as Synology;
`locate` re-checks a node at its mirrored path. Node IDs are generated and bound
to `fs_entries` (identity plus the stat signature the MD5 came from). An entry
keeps its ID when its name and identity match under the same parent, or when its
identity's previous path no longer names it (rename or move); otherwise it is a
new node, which covers replacement and new hard links. Unchanged files are not
rehashed, and a file that changes while hashed is withheld. A mount whose
filesystem ID differs from the one recorded in `fs_mounts` is preserved until
`backend NAME rebind-mount MOUNT`. Contract changes this required: `OperationNotSupportedError`
(mapped to 403 by a middleware) and shared `structural_records`.

The inotify watcher is implemented. Each directory is watched before it is
listed, so changes after a listing always produce an event; events during the
startup scan are applied once it finishes. Watches map to directory node IDs,
not paths, so they follow renames. Events only mark directories dirty; after a
short settle delay, a scoped `ReconcileSession` re-lists them and new
subdirectories, while other subdirectories keep their mirror. A dirty directory
that is gone or replaced is swapped for its parent. Passes are serialized so two
listings cannot mint two IDs for one new entry. Queue overflow triggers a full
rescan; directories that cannot be watched rely on the periodic rescan.

Writes are implemented: create folder, rename, move, permanent delete, and
uploads. Each runs while scan passes are held off and is recorded (write queue
flushed) before they resume, so a pass that lists the result matches the same
node. Renames and upload finalization use `renameat2(RENAME_NOREPLACE)`; a
rename or delete first checks that the mirrored path still names the known
object. Uploads stage `.wcpan-upload-*` files in the destination folder,
hashed while written, and scans skip them. Checksums are now reused while
identity, size, and mtime are unchanged, so renames do not rehash. Contract
change: `OperationInvalidError` (400). `filesystem.read_only` restores the
read-only behaviour.

Not yet done: `open_by_handle_at`. Multi-backend configuration (version 3) is
done; see Compatibility and configuration.

Deliverable: both backends satisfy the common contract without filesystem code
pretending to provide a Synology checkpoint or immutable entry locator.

### 5. Complete distribution boundaries

The workspace initialization establishes independently buildable distributions,
and the hash model and service interface are coordinated across the client
framework, SQLite, providers, crypt, and CLI. Remaining work: migrate duld, dvd, and
drive.py off the removed `cli.lib` (see Client helper packages), keep
client and server dependency sets separate, and validate independent wheel
installations, not only imports within the shared development environment. No
compatibility wrapper is planned for the retired Synology client package.

Status: duld's `wcpan-drive-client` branch was migrated against the earlier
client interface: imports move to `wcpan.drive.client`, `.client.cli`, and
dependencies to `wcpan-drive-client`, `-client-cli`, `-client-feed`, and
`-client-sqlite` (replacing `wcpan-drive-synology`). Its `uv.lock` cannot be
updated until these are published.

That branch predates the client renames (`FileService`/`SnapshotService` to
`Backend`/`Cache`, `create_drive(backend=, cache=)`, provider entry points
`create_backend`/`create_cache`/`create_middleware`, and CLI config version 4
with `backend` and `cache` keys) and does not build against the workspace now.
The client interface is still changing, so duld is deliberately not updated
yet: re-migrate it, and its deployed drive configs, once the interface is
settled, then repeat the validation below.

Last validated, before the renames, with wheels from
`uv build --all-packages --no-sources` installed into clean environments:
duld's dependencies install no server packages, and the server with the
filesystem backend installs no client packages. duld's tests passed there, and
its drive backend created a folder, uploaded, synced, and verified an `md5`
digest against that server.

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

1. Choose release automation and independent versus coordinated version policy
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
