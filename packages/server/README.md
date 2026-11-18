# wcpan-drive-server

Mirrored HTTP file server for wcpan.drive.

The server keeps a SQLite mirror of one or more storage backends and serves it
over HTTP. It holds the mirror, change feed, HTTP API, and reconciliation, and
defines the contract a storage backend implements. Backends are loaded by the
factory path (`type`) in the config, so the core does not depend on any of them.
One server can serve mounts from several backend instances.

Included backends:

- [Synology Drive](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/docs/backend/synology/README.md)
- [Local directories](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/docs/backend/filesystem/README.md)

A third-party backend is a separate package, loaded the same way. See
[Writing a storage backend](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/docs/writing-a-backend.md).

Clients connect with
[`wcpan-drive-client-feed`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-feed/README.md).
The HTTP contract is in the
[protocol spec](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/protocol/SPEC.md).

## Installation

Install the server, which includes its backends, or run it with `uvx`:

```bash
pip install wcpan-drive-server
uvx wcpan.drive.server --config server.yaml serve
```

The database must be on a local filesystem that supports SQLite WAL.

Image and video dimensions are probed with the system
[MediaInfo](https://mediaarea.net/en/MediaInfo) command-line tool, so
installing the wheel alone does not give working probing. Install `mediainfo`
with MediaInfoLib 25.04 or later, built with HTTP (curl) support, on the
server's `PATH` (for example Ubuntu's `mediainfo` package, on a release that
ships 25.04 or later; MediaArea publishes current packages for older ones).
Older releases fail on some WMV files and report no WebP dimensions. The
server checks the version and probes a small image at startup, and does not
start without a working install; offline commands check before their first
probe.

## Command line

Generate the YAML configuration from the installed backend plugins:

```bash
# Generate a starting config for installed backends, then fill in the
# settings marked required
wcpan.drive.server init --list
wcpan.drive.server init --plugin synology --plugin filesystem > server.yaml
wcpan.drive.server --config server.yaml serve

# Offline maintenance; run only while the server is stopped
wcpan.drive.server --config server.yaml gc
wcpan.drive.server --config server.yaml squash
wcpan.drive.server --config server.yaml checkpoint
wcpan.drive.server --config server.yaml backend old-nas drop
wcpan.drive.server --config server.yaml backend nas rebuild

# Commands of one backend instance, parsed by that backend
wcpan.drive.server --config server.yaml backend nas backfill /photos
wcpan.drive.server --config server.yaml backend disk rebind-mount documents
wcpan.drive.server --config server.yaml backend nas pending
wcpan.drive.server --config server.yaml backend nas --help
```

`backend --help` lists the configured instances. A backend's `--help` and
argument errors exit before anything else starts. `gc`, `squash`,
`checkpoint`, and backend commands that write use the same database without
coordinating with a running server; backend commands that only read (each
backend's README says which) write none of its files. Each backend's README
lists its own commands.

`checkpoint` applies pending database compatibility changes and empties the
SQLite WAL left by a crash. Read-only commands ask for it when they find
either, since they write nothing to fix it themselves.

`gc` removes nodes no longer reachable from the server root, and nodes left
under a mount by a backend instance that no longer serves it; each removal is
published to clients. It then forgets the IDs of nodes that are gone.

`backend NAME drop` removes an instance that is no longer in the config: its
nodes (each removal published to clients), node IDs, and backend state. A
configured instance cannot be dropped. `backend NAME rebuild` removes a
configured instance's nodes and backend state but keeps its node IDs; the next
start discovers it again, and items whose backend gives them a stable source
identity get their old IDs back. Both are handled by the server, so a
backend's own commands cannot use these names.

Assigning a mount to another backend instance takes effect on the next start:
the previous instance's nodes under the mount are removed and the new instance
discovers it with new node IDs. The mount itself keeps its ID.

Change cursors are opaque strings bound to the database's change history. `"0"`
starts from the beginning. `squash` replaces the history, so cursors issued
before it, cursors from another database, cursors past the current end (a
restored older copy), and integer cursors from earlier servers get
`410 Gone`; `wcpan-drive-client` then rebuilds that source's cache from `"0"`.

## Config file

```yaml
version: 1
server:
  host: "0.0.0.0"          # default 127.0.0.1
  port: 8080               # default 8080
  database_url: "/data/mirror.db"
  # log_path: "/var/log/wcpan-drive-server.log"
backends:
  nas:
    type: wcpan.drive.server.backend.synology.create_backend
    url: "https://nas.example.com:5001"
    username: "your-drive-user"
    password: "your-password"
    webhook_callback_url: "https://my-server.example.com"
  disk:
    type: wcpan.drive.server.backend.filesystem.create_backend
mounts:
  photos:
    backend: nas
    source: "/volume1/photos"
  documents:
    backend: disk
    source: "/srv/documents"
```

- `backends` names backend instances. `type` is the import path of the
  backend's factory (`module.function`); a third-party backend's package must
  be installed. The
  other keys are that backend's settings, documented in its README.
- `mounts` are single-component names (`photos` appears as `/photos`), each
  served by one backend instance from its `source`.
- Moving a node between mounts of different backends is refused (403); within
  one backend it is up to that backend.
- The server probes image and video dimensions by running MediaInfo on the
  file's bytes, which the backend reads from its source and the server serves
  to MediaInfo over a loopback-only HTTP port. MediaInfo decides how much to
  read, so a probe may transfer most of a large video from the backend. Up to
  two probes run at once, each with a five-minute deadline; a probe that runs
  out of time while waiting for the backend is retried with backoff, one that
  runs out while parsing is recorded as `limit`. A file whose content is
  unchanged keeps its dimensions without a probe. When probing a reachable
  file fails, a file never probed successfully stays hidden; a published file
  is updated and keeps its last probed dimensions. Each backend's README says
  when it retries hidden files and how to list them.
- An upload with complete media info is not probed. Without it, an image or
  video is probed before it is stored: unreadable media is not stored and the
  upload fails with `422 {"error": "invalid_media"}`; any other probe failure
  returns 503, and a resumable upload can be finished again.

Backend settings are checked against the backend factory before it starts:
unknown, missing, or mistyped settings are errors.

The config format and the database schema are at version 1. A database at
another version is rejected, not migrated.

## Backend instances and node IDs

Node IDs are minted by the server and are unique across backend instances,
including several instances of one backend. They are opaque: they do not
encode a backend's own identifiers. Instances of one backend share its tables,
but each reads only its own rows, so two instances over one source never see
each other's nodes. An instance is identified by its name; renaming it only in
the config makes it a new instance, and its backend type cannot change. To
rename one, stop the server and change its `name` in the database's
`backend_instances` table along with the config. An instance
removed from the config keeps its nodes in the mirror, with a warning at
startup, until it is dropped; reading or changing them returns 503.

Each file carries its backend's checksum in `hash`, with the algorithm in
`hash_algorithm`; each backend's README names its algorithm.

## Design

Invariants and their reasons are in
[`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/SPEC.md).
