# wcpan.drive

Client and server packages for asynchronous file access, developed in one uv workspace.

This repository contains:

- a client-side `Backend` implementation that talks to a running server
- a server that mirrors Synology Drive metadata into a local SQLite database
- a Synology API adapter for the server-side mirror

## Workspace

All distributions start at version `0.1.0` under new names. This is a breaking
namespace migration; the old package names and import paths are not aliases.

| Directory | Distribution | Import namespace |
| --- | --- | --- |
| `packages/client` | `wcpan-drive-client` | `wcpan.drive.client` |
| `packages/client-google` | `wcpan-drive-client-google` | `wcpan.drive.client.google` |
| `packages/client-feed` | `wcpan-drive-client-feed` | `wcpan.drive.client.feed` |
| `packages/client-crypt` | `wcpan-drive-client-crypt` | `wcpan.drive.client.crypt` |
| `packages/client-config` | `wcpan-drive-client-config` | `wcpan.drive.client.config` |
| `packages/client-media` | `wcpan-drive-client-media` | `wcpan.drive.client.media` |
| `packages/client-cli` | `wcpan-drive-client-cli` | `wcpan.drive.client.cli` |
| `packages/plugin` | `wcpan-drive-plugin` | `wcpan.drive.plugin` |
| `packages/sqlite` | `wcpan-drive-sqlite` | `wcpan.drive.sqlite` |
| `packages/protocol` | `wcpan-drive-protocol` | `wcpan.drive.protocol` |
| `packages/server` | `wcpan-drive-server` | `wcpan.drive.server` |
| `packages/server-synology` | `wcpan-drive-server-synology` | `wcpan.drive.server.synology` |
| `packages/server-filesystem` | `wcpan-drive-server-filesystem` | `wcpan.drive.server.filesystem` |

The client and server roots are namespace packages. Client framework constructors
are available from `wcpan.drive.client.api`; types and exceptions have explicit
modules. Extensions remain separately installable. The server does not depend on
the client framework; both the server and feed provider use the protocol package.
The client CLI command is `wcpan.drive.client.cli`; it has no library API.
Load drive config files with `wcpan.drive.client.config` and probe local media
with `wcpan.drive.client.media`. Update configured provider
module paths from `wcpan.drive.google` and `.crypt` to
`wcpan.drive.client.google` and `.crypt`, respectively. The SQLite metadata
cache is built into `wcpan-drive-client`; a source names its cache file with
`cache`.

Client caches and server mirrors share `wcpan-drive-sqlite`. Initialization enables
WAL, so database files must be on a local filesystem that supports SQLite WAL.
Existing client caches are switched to WAL without changing their schema.
Connections use a five-second lock timeout, `synchronous=FULL`, SQLite's default
cache size and automatic checkpointing, and disabled foreign-key enforcement;
parent-child consistency remains application-owned.

Read operations use an enforced read-only, deferred transaction for a consistent
snapshot. Write operations use `BEGIN IMMEDIATE`, then commit on success or roll
back on failure. Related reads and writes must use the same connection. The
helpers use `autocommit=True` to disable Python's implicit transaction management
and issue SQL `BEGIN`, `COMMIT`, and `ROLLBACK` explicitly. Callers must not commit,
roll back, or include transaction commands in schema scripts. Cancelling an async
caller does not stop work already running in an executor. Schemas, executor
ownership, and the server write queue remain package-owned.

The generic server (`wcpan-drive-server`) holds the mirror, change feed, HTTP
API, and reconciliation, and defines the contract a storage backend implements.
It provides the `wcpan.drive.server` command and does not depend on any
backend. Backends are separate packages, loaded by the module name in the
config: `wcpan-drive-server-synology` (Synology Drive) and
`wcpan-drive-server-filesystem` (local directories). One server can serve
mounts from several backend instances.

Client providers and server backends are plugins (`wcpan-drive-plugin`): each
factory's keyword-only parameters are its config settings, checked against
their types when the config is loaded, and each package registers its factories
as entry points so `init` can generate a starting config. Server backend
settings are flat keys, except mappings such as `local_paths`.

Hash algorithms are per file. Nodes carry a digest in `hash` and its algorithm in
`hash_algorithm` (both empty for directories and for files without a checksum).
Each provider defines its identifiers: Google reports `md5`, the Synology backend
reports `md4`, and the feed client accepts `md4`, `md5`, `sha1`, and `sha256`. Use `Drive.get_hasher_factory(node)` to verify
a file; it raises `UnsupportedHashAlgorithmError` instead of guessing. The client
service API version is 6. Existing client SQLite caches (schema 6) are
rejected and must be rebuilt or migrated manually to schema 7.

The server database is at schema 5 and is not migrated from older schemas.
Schema 5 mints node IDs in the server and gives each backend instance its own
tables, so every node ID changes. To upgrade, stop the server and move the
database file, together with any `-wal` and `-shm` files, aside; the server
creates a new database and runs initial discovery. Clients resynchronize on
their own: their old cursors are stale. Consumers that store node IDs must
look them up again. Keep the old file to roll back.

Node payloads no longer expose the Synology `file_id` or the `mutable_id` key.
Clients from before this migration require that key and cannot read current
responses; current clients ignore it from older servers.

```bash
uv sync --all-packages --locked
make test
make lint
uv build --all-packages --no-sources --clear
```

Each member has its own version and release artifacts. The workspace root is not
published. For a selected release, build into a dedicated directory and publish
only those artifacts:

```bash
uv build --package wcpan-drive-client --no-sources --out-dir dist/client
uv publish dist/client/*
```

Member unit tests run in separate processes because their `tests` package names
can overlap. Root tests retain the original cross-package regression suite.

## Installation

```bash
pip install wcpan-drive-client-feed
```

For the server, install the core with the backends your config uses, or run it
with `uvx`:

```bash
pip install wcpan-drive-server wcpan-drive-server-synology
uvx --with=wcpan-drive-server-synology --with=wcpan-drive-server-filesystem \
    wcpan.drive.server --config server.yaml serve
```

## Usage

### Client

The client connects to a running `wcpan.drive.server` server instance and
implements the `wcpan.drive.client` `Backend` interface.

```python
from wcpan.drive.client.feed import create_backend

async with create_backend(server_url="http://localhost:8080") as backend:
    root = await backend.get_root()
    print(f"Root: {root}")
```


#### Querying cached nodes

Use `drive.find_nodes()` to combine filters over the local cache. Await `.all()`
for matching nodes, or `.summarize()` for `NodeSummary(count, total_size)` without
loading node objects:

```python
summary = await (
    drive.find_nodes()
    .files_only()
    .include_trashed()
    .created(after=begin, before=end)
    .summarize()
)
print(summary.total_size)
```

Creation windows include `begin` and exclude `end`; times must be timezone-aware.
Queries exclude trashed nodes by default. Summaries count all matching nodes;
directories contribute zero bytes. No matches produce zero for both fields.
Use `.descendant_of(node)` to restrict a query to a directory's descendants,
excluding the directory itself. Across sources, summaries combine their totals.
Summaries reject `.limit()` because there is no defined ordering for a subset.

This replaces the SQLite-specific `get_uploaded_size` helper. The example sums
cached file sizes by creation time; it does not track upload traffic or files
that have been permanently deleted. CLI `usage` (`du`) also uses cached summaries.

### Server

The server keeps a SQLite mirror of one or more storage backends and serves it
over HTTP. Generate its YAML configuration from the installed backend plugins:

```bash
# Generate a starting config for installed backends, then fill in the
# settings marked required
wcpan.drive.server init --list
wcpan.drive.server init --plugin synology --plugin filesystem > server.yaml
wcpan.drive.server --config server.yaml serve

# Offline maintenance; run only while the server is stopped
wcpan.drive.server --config server.yaml gc
wcpan.drive.server --config server.yaml squash
wcpan.drive.server --config server.yaml backend old-nas drop
wcpan.drive.server --config server.yaml backend nas rebuild

# Commands of one backend instance, parsed by that backend
wcpan.drive.server --config server.yaml backend nas backfill /photos
wcpan.drive.server --config server.yaml backend disk rebind-mount documents
wcpan.drive.server --config server.yaml backend nas --help
```

`backend --help` lists the configured instances. A backend's `--help` and
argument errors never connect to its source. `gc`, `squash`, and backend
commands write the same database without coordinating with a running server.

`gc` removes nodes no longer reachable from the server root, and nodes left
under a mount by a backend instance that no longer serves it; each removal is
published to clients. It then forgets the IDs of nodes that are gone.

`backend NAME drop` removes an instance that is no longer in the config: its
nodes (each removal published to clients), node IDs, and backend state. A
configured instance cannot be dropped. `backend NAME rebuild` removes a
configured instance's nodes and backend state but keeps its node IDs; the next
start discovers it again, and items with a stable source identity (Synology
permanent links) get their old IDs back. Both are handled by the server, so a
backend's own commands cannot use these names.

Assigning a mount to another backend instance takes effect on the next start:
the previous instance's nodes under the mount are removed and the new instance
discovers it with new node IDs. The mount itself keeps its ID.

Change cursors are opaque strings bound to the database's change history. `"0"`
starts from the beginning. `squash` replaces the history, so cursors issued
before it, cursors from another database, cursors past the current end (a
restored older copy), and integer cursors from earlier servers get
`410 Gone`; `wcpan-drive-client` then rebuilds that source's cache from `"0"`.

#### Config file

```yaml
version: 3
server:
  host: "0.0.0.0"          # default 127.0.0.1
  port: 8080               # default 8080
  database_url: "/data/mirror.db"
  # log_path: "/var/log/wcpan-drive-server.log"
backends:
  nas:
    type: wcpan.drive.server.synology.create_backend
    url: "https://nas.example.com:5001"
    username: "your-drive-user"
    password: "your-password"
    webhook_callback_url: "https://my-server.example.com"
    local_paths:
      "/volume1/photos": "/mnt/nas/photos"
  disk:
    type: wcpan.drive.server.filesystem.create_backend
mounts:
  photos:
    backend: nas
    source: "/volume1/photos"
  documents:
    backend: disk
    source: "/srv/documents"
```

- `backends` names backend instances. `type` is the import path of the
  backend's factory (`module.function`); its package must be installed. The other keys are that backend's settings.
- `mounts` are single-component names (`photos` appears as `/photos`), each
  served by one backend instance from its `source`.
- Moving a node between mounts of different backends is refused (403); within
  one backend it is up to that backend.
- `local_paths` (per backend) maps source path prefixes to host paths for
  probing image and video dimensions. When probing a reachable file fails, the
  change is withheld: a new file stays hidden and a changed file keeps its
  previous version. Each server start retries withheld files.

Version 2 files, which configured a single Synology server with top-level
`synology`, `mounts`, and `local_paths`, are still accepted and read as one
backend named `synology`.

Backend settings are checked against the backend factory before it starts:
unknown, missing, or mistyped settings are errors.

Node IDs are minted by the server and are unique across backend instances,
including several instances of one backend. They are opaque: a Synology node's
ID is not its permanent link. Instances of one backend share its tables, but
each reads only its own rows, so two Synology servers or two filesystem
instances over one disk never see each other's nodes. An instance is identified by its name; renaming it in the config
makes it a new instance, and its backend type cannot change. An instance
removed from the config keeps its nodes in the mirror, with a warning at
startup, until it is dropped; reading or changing them returns 503.

#### Synology backend

Settings: `url`, `username`, `password`, `webhook_callback_url`, optional
`webhook_app_id` (default `wcpan-drive-server-synology`), optional `otp_code`,
optional `local_paths`, and optional `upload_tmp_dir`. Version 3 configs with a
nested `webhook` section (`callback_url`, `app_id`) are rejected; rename its
keys to `webhook_callback_url` and `webhook_app_id`. `upload_tmp_dir` used to
be a `server` setting; version 3 configs that still set `server.upload_tmp_dir`
are rejected, so move it to each Synology backend. Version 2 files are
converted. Each instance:

- connects to Synology Drive through DSM's WebStation API using
  `wcpan-synology`
- registers a webhook at `WEBHOOK_CALLBACK_URL/api/v1/backends/NAME/webhook`, where
  `NAME` is the backend instance name
- keeps its mounts in sync through a startup scan, webhook events, and the
  `backend NAME backfill PATH` command
- spools resumable uploads to a temporary file under `upload_tmp_dir`
  (default: the system temporary directory), since Synology Drive takes a file
  in one request; the file is uploaded when the last chunk arrives. Direct
  uploads stream straight through. Each server start removes spool
  directories that no running server holds, such as those left by a crash. If Synology Drive accepted the file but the
  server failed before recording it, finishing the session again reports the
  upload as a conflict with that file

`backfill PATH` takes a virtual mirror path (`/` is the server root, then mount
names and folders) and reconciles this instance's mounts under it.

#### Filesystem backend

Serves local directories; mount sources are absolute host directories.
Settings, all optional:

```yaml
  disk:
    type: wcpan.drive.server.filesystem.create_backend
    rescan_interval: 3600  # seconds between full rescans; default 3600
    watch: true            # follow changes with inotify; default true
    read_only: false       # reject every change through the server; default false
    # local_paths:         # default: probe media at the source paths
```

- Discovery is a full scan at startup, then inotify: a changed directory is
  re-read about a second after its last change. Full rescans every
  `rescan_interval` seconds catch anything missed, such as writes through
  `mmap` or directories that could not be watched; if the kernel drops events,
  a full rescan runs immediately. Each watched directory uses one inotify
  watch; raise `fs.inotify.max_user_watches` for large trees. With
  `watch: false`, changes appear only after the next rescan.
- Regular files and directories are mirrored; symlinks and special files are
  skipped. Each directory entry is its own node, so hard links appear once per
  link.
- File checksums are MD5, computed by the server and reused while a file's
  size and modification time are unchanged (a rename does not rehash). A file that changes while being hashed
  keeps its previous version until the next scan.
- Node IDs follow the object, not the path, using Linux file handles (ext4 is
  the first supported filesystem; others fall back to inode numbers). Renames
  and moves keep their IDs; a replaced file gets a new one.
- Clients can create folders, rename, move, delete, and upload. Deletes are
  permanent; deleting a folder removes its contents. Renames, moves, and
  uploads never replace an existing entry: a taken name returns 409 with the
  existing node where possible. Invalid names (empty, `.`, `..`, or containing
  `/`) return 400, and permission errors return 403. A node whose path now
  names a different file is not renamed or deleted (503); retry after the next
  scan. Moves across filesystems are not supported.
- Uploads are written to a hidden `.wcpan-upload-*` file in the destination
  folder and renamed into place when complete; interrupted uploads are removed,
  and these files are never mirrored. Resumable upload sessions write there
  directly. If the folder moves during a session, finishing it returns 503
  until a scan has seen the move; if the folder is deleted, it returns 410.
  Full scans delete staging files that no upload of this server holds and that
  have not been written for two hours, such as those left by a crash; with
  `read_only: true` they are kept.
- With `read_only: true`, every change returns 403.
- Images and videos are probed from the mount sources unless `local_paths` maps
  them elsewhere.
- A mount whose source is missing, unreadable, or now on a different
  filesystem (for example an unmounted disk) keeps its mirror instead of being
  emptied. If the filesystem change is intended, stop the server and run
  `backend NAME rebind-mount MOUNT`.

## Requirements

- Python >= 3.13
- aiohttp >= 3.13.0
- pycryptodome >= 3.0
- Workspace client framework: wcpan-drive-client >= 0.1.0
- wcpan-synology >= 0.2.0

Server packages additionally require: dacite, pyyaml, pymediainfo, wcpan-logging.

## Development

Code style rule for function signatures:

- Keep semantic inputs positional when they are the subject of the operation.
- Make infrastructure and wiring parameters keyword-only when they act as facilities for the function or object, such as `storage`, `drive_api`, `network`, `node_sync`, `off_main`, or similar service/session dependencies.

Example:

```python
await copy_node(src, dst, node_sync=node_sync)
```

Useful commands:

```bash
make test
make lint
make format
make coverage
```

## License

MIT
