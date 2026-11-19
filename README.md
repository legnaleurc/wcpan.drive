# wcpan.drive

Asynchronous file access through one drive interface, with an optional
self-hosted file server. Client and server packages are developed together in
one uv workspace and published separately.

- The client framework (`wcpan-drive-client`) exposes one or more storage
  sources as a single drive, with a local SQLite metadata cache. Providers
  connect it to Google Drive or to the server; middleware such as encryption
  wraps a provider.
- The server (`wcpan-drive-server`) mirrors one or more storage backends into
  a local SQLite database and serves the mirror over HTTP. Backends serve
  Synology Drive or local directories.

The server is an independent file service, not a remote client: the two sides
meet only at the HTTP contract in `wcpan-drive-protocol`.

## Packages

Each package's README documents its usage and configuration.

| Package | Import namespace | Purpose |
| --- | --- | --- |
| **Applications** | | |
| [`wcpan-drive-client-cli`](packages/client-cli/README.md) | `wcpan.drive.client.cli` | Command line client |
| [`wcpan-drive-server`](packages/server/README.md) | `wcpan.drive.server` | Mirror server, HTTP API, and the server command |
| **Client framework and providers** | | |
| [`wcpan-drive-client`](packages/client/README.md) | `wcpan.drive.client` | Drive framework, cache, and sync |
| [`wcpan-drive-client-config`](packages/client-config/README.md) | `wcpan.drive.client.config` | YAML drive configuration |
| [`wcpan-drive-client-feed`](packages/client-feed/README.md) | `wcpan.drive.client.feed` | Provider for the server |
| [`wcpan-drive-client-google`](packages/client-google/README.md) | `wcpan.drive.client.google` | Google Drive provider |
| [`wcpan-drive-client-crypt`](packages/client-crypt/README.md) | `wcpan.drive.client.crypt` | Encryption middleware |
| [`wcpan-drive-client-media`](packages/client-media/README.md) | `wcpan.drive.client.media` | Local MIME type and media probing |
| **Server backends (in `wcpan-drive-server`)** | | |
| [Synology Drive](packages/server/docs/backend/synology/README.md) | `wcpan.drive.server.backend.synology` | Synology Drive backend |
| [Local directories](packages/server/docs/backend/filesystem/README.md) | `wcpan.drive.server.backend.filesystem` | Local directory backend |
| **Shared** | | |
| [`wcpan-drive-protocol`](packages/protocol/README.md) | `wcpan.drive.protocol` | HTTP wire types |
| [`wcpan-drive-sqlite`](packages/sqlite/README.md) | `wcpan.drive.sqlite` | SQLite connection and transaction helpers |
| [`wcpan-drive-plugin`](packages/plugin/README.md) | `wcpan.drive.plugin` | Plugin discovery and factory settings |

Client and server installs are disjoint: only `protocol`, `sqlite`, and
`plugin` are shared.

## Getting started

- **Use a drive from Python:** install `wcpan-drive-client` with a provider and
  start from the [client README](packages/client/README.md).
- **Use a drive from the command line:** install `wcpan-drive-client-cli` with
  a provider; see the [CLI README](packages/client-cli/README.md).
- **Run a server:** install `wcpan-drive-server`, which includes its backends;
  see the [server README](packages/server/README.md). Clients connect
  to it with [`wcpan-drive-client-feed`](packages/client-feed/README.md).

## Documentation

- [`docs/architecture.md`](docs/architecture.md): how the members fit together
  and who owns what.
- Each member's `SPEC.md`: its design invariants and the reasons for them.
  Built-in server backends keep theirs in `packages/server/docs/backend/`.

## Development

```bash
uv sync --all-packages --locked
make test
make lint
make format
make coverage
```

Member unit tests run in separate processes because their `tests` package names
can overlap. Root tests cover the client against the server.

Code style rule for function signatures:

- Keep semantic inputs positional when they are the subject of the operation.
- Make infrastructure and wiring parameters keyword-only when they act as facilities for the function or object, such as `storage`, `drive_api`, `network`, `writer`, `off_main`, or similar service/session dependencies.

Example:

```python
await copy_node(src, dst, writer=writer)
```

### Releasing

Each member has its own version and release artifacts. The workspace root is not
published. Check that every member builds:

```bash
uv build --all-packages --no-sources --clear
```

For a selected release, build into a dedicated directory and publish only those
artifacts:

```bash
uv build --package wcpan-drive-client --no-sources --out-dir dist/client
uv publish dist/client/*
```

Member READMEs are their PyPI descriptions, so links in them to other files of
this repository must be absolute URLs.

## License

MIT — see [LICENSE.txt](LICENSE.txt). Each package under `packages/` ships an identical copy.
