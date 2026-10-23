# wcpan.drive

Client and server packages for asynchronous file access, developed in one uv workspace.

This repository contains:

- a client-side `FileService` implementation that talks to a running server
- a server that mirrors Synology Drive metadata into a local SQLite database
- a Synology API adapter for the server-side mirror

## Workspace

All distributions start at version `0.1.0` under new names. This is a breaking
namespace migration; the old package names and import paths are not aliases.

| Directory | Distribution | Import namespace |
| --- | --- | --- |
| `packages/client` | `wcpan-drive-client` | `wcpan.drive.client` |
| `packages/client-google` | `wcpan-drive-client-google` | `wcpan.drive.client.google` |
| `packages/client-remote` | `wcpan-drive-client-remote` | `wcpan.drive.client.remote` |
| `packages/client-crypt` | `wcpan-drive-client-crypt` | `wcpan.drive.client.crypt` |
| `packages/client-sqlite` | `wcpan-drive-client-sqlite` | `wcpan.drive.client.sqlite` |
| `packages/client-cli` | `wcpan-drive-client-cli` | `wcpan.drive.client.cli` |
| `packages/protocol` | `wcpan-drive-protocol` | `wcpan.drive.protocol` |
| `packages/server` | `wcpan-drive-server` | `wcpan.drive.server` |
| `packages/server-synology` | `wcpan-drive-server-synology` | `wcpan.drive.server.synology` |

The client and server roots are namespace packages. Client framework constructors
are available from `wcpan.drive.client.api`; types and exceptions have explicit
modules. Extensions remain separately installable. The server does not depend on
the client framework; both the server and remote provider use the protocol package.
The Synology distribution supplies the `wcpan.drive.server` command.
The client CLI command is `wcpan.drive.client.cli`. Update configured provider
module paths from `wcpan.drive.google`, `.crypt`, and `.sqlite` to
`wcpan.drive.client.google`, `.crypt`, and `.sqlite`, respectively.

This initialization preserves the existing Synology-backed behavior. Backend
contracts and generic reconciliation remain planned work in `PLAN.md`; the server
still contains Synology-specific orchestration and requires the Synology adapter
to run. No filesystem backend is shipped yet.

Hash algorithms are per file. Nodes carry a digest in `hash` and its algorithm in
`hash_algorithm` (both empty for directories and for files without a checksum).
Each provider defines its identifiers: Google reports `md5`, the Synology backend
reports `md4`, and the remote client accepts `md4`, `md5`, `sha1`, and `sha256`. Use `Drive.get_hasher_factory(node)` to verify
a file; it raises `UnsupportedHashAlgorithmError` instead of guessing. The client
service API version is 6. Existing client SQLite snapshots (schema 6) are
rejected and must be rebuilt or migrated manually to schema 7.

The server database is at schema 6 and migrates older databases (schema 3 to 5)
automatically at startup. Schema 4 marks existing Synology digests as `md4`;
schema 5 moves each node's Synology `file_id` out of the public node table into
backend-owned locator storage; schema 6 adds a table of files whose media probe
failed. Node IDs and the change feed are preserved. The
migration cannot be undone in place, so stop the server and copy the database
file, together with any `-wal` and `-shm` files, before upgrading; restore that
copy to roll back.

Node payloads no longer expose the Synology `file_id`. Responses keep a
deprecated, always-empty `mutable_id` key for older decoders that require it, and
current clients accept payloads without it.

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
pip install wcpan-drive-client-remote
```

For the server component:

```bash
pip install wcpan-drive-server-synology
```

## Usage

### Client

The client connects to a running `wcpan.drive.server` server instance and
implements the `wcpan.drive.client` `FileService` interface.

```python
from wcpan.drive.client.remote import create_service

async with create_service(server_url="http://localhost:8080") as file_service:
    root = await file_service.get_root()
    print(f"Root: {root}")
```

### Server

The server mirrors a Synology Drive instance locally via `wcpan-synology` and a
webhook-driven update pipeline. Configure it with a YAML file (see
`server.example.yaml`):

```bash
# Copy and edit the example config
cp server.example.yaml server.yaml

# Start the server
wcpan.drive.server --config server.yaml serve

# Other subcommands
wcpan.drive.server --config server.yaml gc
wcpan.drive.server --config server.yaml backfill /
wcpan.drive.server --config server.yaml squash
```

At startup, the server:

- connects to Synology Drive through DSM's WebStation API using
  `wcpan-synology`
- registers a webhook at `CALLBACK_URL/api/v1/synology-webhook`
- serves a local HTTP API for the client integration
- keeps a SQLite-backed mirror in sync through scans, webhook events, and
  explicit backfill commands

`backfill PATH` operates on the server's virtual mirror paths. `/` is the
server root; mounted Synology paths appear underneath it by mount label.

`gc`, `backfill`, and `squash` are offline maintenance commands. Run them only
while the server is stopped; they write the same database and are not
coordinated with a running server.

#### Config file

```yaml
version: 2
host: "0.0.0.0"
port: 8080
database_url: "sqlite:////data/mirror.db"
synology:
  url: "https://nas.example.com:5001"
  username: "your-drive-user"
  password: "your-password"
  webhook:
    callback_url: "https://my-server.example.com"
mounts:
  photos: "/volume1/photos"
local_paths: {}
```

`version` is the config schema version and must match the version supported by
the current binary. The current binary accepts only version `2`. `local_paths`
is currently required by the config schema; use `{}` when local media probing
is not needed. `synology.webhook.app_id` defaults to `wcpan-drive-server-synology`. See
`server.example.yaml` for the full set of options.

Important fields:

- `database_url`: SQLite database location for the local mirror
- `synology.url`: DSM / Synology Drive base URL
- `mounts`: virtual mount label to Synology path mapping
- `local_paths`: optional Synology-path to local-filesystem mapping for media
  probing; required by the current schema, so use `{}` if unused. When probing
  a reachable image or video fails, the change is withheld: a new file stays
  hidden and a changed file keeps its previous version. Each server start
  retries withheld files.
- `synology.webhook.callback_url`: externally reachable base URL used for
  webhook registration
- `synology.otp_code`: optional 2FA code when the Synology account requires it

Synology API behavior, reference types, and Synology-side exceptions are
provided by `wcpan-synology`, which is only needed by the server packages. The client never raises `wcpan.synology` exceptions; it raises the
errors in `wcpan.drive.client.remote.exceptions` (`SynologyServerError` and its
subclasses for access, upload, and name-too-long failures), which describe the
HTTP client/server boundary.

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
