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
| `packages/client-sqlite` | `wcpan-drive-client-sqlite` | `wcpan.drive.client.sqlite` |
| `packages/client-config` | `wcpan-drive-client-config` | `wcpan.drive.client.config` |
| `packages/client-media` | `wcpan-drive-client-media` | `wcpan.drive.client.media` |
| `packages/client-cli` | `wcpan-drive-client-cli` | `wcpan.drive.client.cli` |
| `packages/plugin` | `wcpan-drive-plugin` | `wcpan.drive.plugin` |
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
module paths from `wcpan.drive.google`, `.crypt`, and `.sqlite` to
`wcpan.drive.client.google`, `.crypt`, and `.sqlite`, respectively.

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

The server database is at schema 7 and migrates older databases (schema 3 to 6)
automatically at startup. Schema 4 marks existing Synology digests as `md4`;
schema 5 moves each node's Synology `file_id` out of the public node table into
backend-owned locator storage; schema 6 adds a table of files whose media probe
failed; schema 7 records which backend instance each of those belongs to.
Node IDs and the change feed are preserved. The
migration cannot be undone in place, so stop the server and copy the database
file, together with any `-wal` and `-shm` files, before upgrading; restore that
copy to roll back.

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

### Server

The server keeps a SQLite mirror of one or more storage backends and serves it
over HTTP. Configure it with a YAML file (see `server.example.yaml`):

```bash
# Generate a starting config for installed backends, then fill in the
# settings marked required (or copy server.example.yaml)
wcpan.drive.server init --list
wcpan.drive.server init --plugin synology --plugin filesystem > server.yaml
wcpan.drive.server --config server.yaml serve

# Offline maintenance; run only while the server is stopped
wcpan.drive.server --config server.yaml gc
wcpan.drive.server --config server.yaml squash

# Commands of one backend instance, parsed by that backend
wcpan.drive.server --config server.yaml backend nas backfill /photos
wcpan.drive.server --config server.yaml backend disk rebind-mount documents
wcpan.drive.server --config server.yaml backend nas --help
```

`backend --help` lists the configured instances. A backend's `--help` and
argument errors never connect to its source. `gc`, `squash`, and backend
commands write the same database without coordinating with a running server.

#### Config file

```yaml
version: 3
server:
  host: "0.0.0.0"          # default 127.0.0.1
  port: 8080               # default 8080
  database_url: "/data/mirror.db"
  # log_path: "/var/log/wcpan-drive-server.log"
  # upload_tmp_dir: "/tmp/wcpan-uploads"
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

Node IDs from different backends share one namespace: filesystem IDs are random
hex and Synology IDs are its permanent links, so collisions are not expected.

#### Synology backend

Settings: `url`, `username`, `password`, `webhook_callback_url`, optional
`webhook_app_id` (default `wcpan-drive-server-synology`), optional `otp_code`,
and optional `local_paths`. Version 3 configs with a nested `webhook` section
(`callback_url`, `app_id`) are rejected; rename its keys to
`webhook_callback_url` and `webhook_app_id`. Version 2 files are converted.
Each instance:

- connects to Synology Drive through DSM's WebStation API using
  `wcpan-synology`
- registers a webhook at `WEBHOOK_CALLBACK_URL/api/v1/backends/NAME/webhook`, where
  `NAME` is the backend instance name
- keeps its mounts in sync through a startup scan, webhook events, and the
  `backend NAME backfill PATH` command

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
  and these files are never mirrored.
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
