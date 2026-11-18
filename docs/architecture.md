# Architecture

How the workspace members fit together, and where each design rule lives. A
member's invariants are in its `SPEC.md` where it has one; user-facing usage is
in its `README.md`.

This map and the specs describe ownership, contracts, and design rationale.
Keep private module layouts and exact algorithms in code, expected behavior in
tests, and historical investigations in Git history. Reference files when they
provide a useful entry point, rather than maintaining implementation inventories.

## Shape

```text
Synology Drive / local directories
    -> server backend (server: backend.synology, backend.filesystem)
    -> server core (server): mirror, change feed, HTTP API
    -> HTTP contract (protocol)
    -> feed provider (client-feed)
    -> client framework (client): Drive, cache, sync
    -> applications (client-cli)
```

The server is an independent file service, not a remote `wcpan.drive.client`.
Each side adapts to the HTTP contract, and neither depends on the other's model
(`packages/protocol/SPEC.md`). The public mirror uses opaque node IDs minted by
the server, each naming one node for the life of the server database. Backend locators, event sources, and discovery progress stay private
to each backend.

## Members and dependencies

| Member | Depends on (workspace) | Owns |
| --- | --- | --- |
| `protocol` | nothing | Wire types and (de)serialization |
| `sqlite` | nothing | Connection and transaction helpers |
| `plugin` | nothing | Factory settings, entry point discovery |
| `client` | `sqlite` | Drive framework, contracts, cache, sync, `client.lib` helpers |
| `client-google` | `client` | Google Drive provider |
| `client-feed` | `client`, `protocol` | Provider for the HTTP server |
| `client-crypt` | `client` | Encryption middleware |
| `client-config` | `client`, `plugin` | YAML drive configuration |
| `client-media` | `client` | Local MIME type and media probing (needs native `libmagic`, `libmediainfo`) |
| `client-cli` | `client`, `client-config`, `client-media`, `plugin` | The CLI command; no library API |
| `server` | `sqlite`, `protocol`, `plugin`, external `wcpan-synology` | Mirror, change log, HTTP API, reconciliation, backend contract, the server command, the Synology and local directory backends |

Rules:

- The server core never imports a backend, `wcpan.synology`, or client packages.
  Backends are loaded by their factory path from the config.
- Client and server installs stay disjoint: a client application installs no
  server package, and a server installs no client package. Only `protocol`,
  `sqlite`, and `plugin` are shared.
- Backends import the core only through its public modules: `types`
  (the backend contract and the core services a backend receives),
  `exceptions`, and `lib`. Changing a name exported there changes the backend
  contract; everything else in the core is private. Built-in backends live under
  `wcpan.drive.server.backend` and follow the same rule as a third-party backend,
  which can live under that namespace or in any other module.
- `client` and `server` are namespace roots with explicit public modules; there
  is no separate core distribution. Extract one only if dependency or release
  needs justify it.
- Each member is versioned and published separately; the root is not published.

## Ownership

Server core (`packages/server/SPEC.md`):

- Node model, the virtual root and mounts, and public node IDs.
- The SQLite mirror, change log, tombstones, cursor validity, and `gc`.
- Reconciliation sessions, observation ordering, and conditional removal.
- Atomic application of observations with backend state.
- HTTP handlers, error translation, upload sessions, readiness, and lifecycle
  coordination of backend instances, including range serving for downloads
  and media probing.
- The SQLite executor and write queue.
- Media probing (the system MediaInfo executable reading backend sources over
  a loopback HTTP service) and publication rules.

Backend (`packages/server/docs/backend/*/SPEC.md`):

- Identity rules and locator meaning, including reverse lookup from native
  events.
- Listing, metadata, create, rename/move, delete, download, and upload against
  the source, with native errors translated to the server's errors.
- Discovery strategy, notifications, debounce, and recovery from lost events.
- Checkpoints: interpretation, persistence, and reset rules.
- Source validation and path, name, mount, and symlink policies.
- Its own routes (registered through the core's route registry), settings,
  credentials, checksums, and I/O pools.
- The media source for its files: exact ranged reads bound to the observed
  state, and how a failed probe is retried.

The server reports readiness as a whole: it is ready once every instance has
finished initial discovery.

Client side:

- `client` owns the `Drive` contracts, the per-source cache, stale cursor
  recovery, and transfer and checksum helpers. Their invariants belong in
  `packages/client/SPEC.md`.
- Applications and the CLI own application wiring and execution pools.
- Providers and middleware adapt their sources to `Backend`. A provider that
  talks to a storage service defines its algorithm identifiers (Google: `md5`);
  `client-feed` interprets the identifiers the server's backends define, and
  `client-crypt` wraps whatever hasher it is given.

## Adding code

A new helper goes to the member that owns its responsibility. A new member is
justified when consumers need to install a responsibility independently;
dependency weight is a secondary reason, and being stdlib-only does not by
itself make something core.
