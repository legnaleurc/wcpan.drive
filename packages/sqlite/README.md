# wcpan-drive-sqlite

Shared synchronous SQLite connection and transaction helpers for the client and server.

Initialization enables WAL, so database files must be on a local filesystem
that supports SQLite WAL. Existing client caches are switched to WAL without
changing their schema. Connections use a five-second lock timeout,
`synchronous=FULL`, SQLite's default cache size and automatic checkpointing,
and disabled foreign-key enforcement; parent-child consistency remains
application-owned.

See [`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/sqlite/SPEC.md) for connection settings and
transaction semantics.
