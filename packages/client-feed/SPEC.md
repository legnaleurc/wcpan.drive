# wcpan-drive-client-feed design

Adapts the server's HTTP contract (`packages/protocol/SPEC.md`) to the
`wcpan.drive.client` `Backend` interface.

## Cursors

- `get_initial_cursor()` is `"0"`, the server's bootstrap cursor, so the initial
  cursor is never stale.
- Cursors returned by the server are stored and sent back unchanged.
- `410 Gone` on `/changes` raises `StaleCursorError`. Recovery belongs to the
  client framework (`packages/client/SPEC.md`), not to this package.

## Requests

- Every node ID placed in a URL path is percent-encoded with no safe characters
  (`node_url`), because the protocol allows any character in IDs.
- For `create_directory`, a 409 with a node body becomes `NodeExistsError`
  carrying that node, or returns the existing node when `exist_ok` is set. A 409
  without a node body is a server error. Uploads map a 409 node body to
  `NodeExistsError` the same way.
- For `get_root`, `get_changes`, `move`, and `delete`: 404 becomes
  `NodeNotFoundError`; 401, 403, and 429 become an access error; other failures
  become a server error carrying the status. `move` does not decode 409 bodies.
- Downloads surface aiohttp errors; uploads use the upload errors below. A
  read after a seek accepts only a range answer for the requested positions
  (RFC 9110 §14.4, §15.3.7), and fails with a server error otherwise. The total
  in `Content-Range` is not checked: the server's copy may have changed size.
- The protocol has no trash and no permanent flag, so the server decides what a
  delete means (`packages/protocol/SPEC.md`). `delete` ignores `permanent`,
  `restore` is not supported, and `purge_trash` does nothing.

## Checksums

The hasher is selected from each node's `hash_algorithm` as reported by the
server, never hard-coded. Supported identifiers are `md4` (Synology), `md5`
(filesystem), `sha1`, and `sha256`, each a lowercase hex digest of the whole
file. `md4` uses PyCryptodome, since hashlib often lacks it under OpenSSL 3.

## Uploads

Resumable uploads keep a local copy of what was written (in memory up to 64 MiB,
then on disk), so they can resend from the server's `Upload-Offset` after a
mismatch or a transient failure.

- A 409 with an `Upload-Offset` header is an offset mismatch: the upload resends
  from that offset. At the full size the server is publishing, so the upload
  fails at once; the caller syncs and compares the hash of what appears. At the
  offset just sent, an earlier request still holds the session: the upload
  backs off and retries like a 503.
- 503 is retried, honoring `Retry-After` when present.
- 422, 507, and other 4xx responses are permanent; the upload is not retried.
- A 404 for the session means the server lost it (for example, after a restart),
  so the upload cannot be resumed.

The exception classes are named `Synology*` because the server started as a
Synology proxy; they apply to every backend.
