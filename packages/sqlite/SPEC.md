# wcpan-drive-sqlite design

Connection and transaction helpers shared by the client metadata cache and the
server mirror. Schemas, executors, and write queues belong to the callers. Tests
pin the exact behavior.

## Connections

- `initialize` switches the database to persistent WAL mode and fails if SQLite
  does not accept it. Database files must be on a local filesystem that supports
  WAL.
- Every connection runs with `autocommit=True`, so Python's implicit transaction
  management is off and the helpers issue `BEGIN`, `COMMIT`, and `ROLLBACK`
  themselves.
- Connections use a five-second busy timeout, `synchronous=FULL`, SQLite's default
  cache size, and automatic checkpointing.
- Foreign-key enforcement is off. Parent-child consistency and cleanup of
  dependent rows are owned by the application (see the server's spec).
- Each helper opens a connection and closes it when the block ends, so related
  reads and writes must happen inside one block.
- Connections open with `uri=True`, so a DSN may be a `file:` URI; plain paths
  and `:memory:` are unaffected.
- `read_only_dsn(path)` opens with `mode=ro&immutable=1`, which writes no file:
  `mode=ro` alone still creates or updates `-wal`/`-shm`. Immutable opens
  ignore the WAL and locks, so they are correct only while nothing writes the
  database and `has_pending_wal(path)` is false (a zero-byte WAL holds
  nothing). Both resolve symbolic links, since SQLite names the WAL files
  after the real file. `uses_wal(path)` reads WAL mode from the file header,
  which an immutable connection cannot report.

## Transactions

- `read_only` sets `query_only` and opens a deferred transaction, which gives a
  consistent snapshot and rejects accidental writes.
- `read_write` opens with `BEGIN IMMEDIATE`. It commits when the block exits
  normally and rolls back on any exception, including cancellation.
- Callers must not end the transaction or put transaction commands in schema
  scripts. Because of `autocommit=True`, `executescript` does not commit on its
  own.

Writes take the write lock at `BEGIN`, not at the first write statement. The
reads in a check-then-write block are part of the write transaction. A competing
writer waits at `BEGIN` for up to the busy timeout and fails only if the lock is
still held after that. A deferred
transaction that later upgrades to a write in WAL mode fails immediately with
`SQLITE_BUSY` when another connection committed since its snapshot, and the busy
timeout does not apply. The client cache has no single writer, so this matters.

Cancelling an async caller does not stop work already running in an executor; the
transaction still commits or rolls back on that thread.
