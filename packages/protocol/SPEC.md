# wcpan-drive-protocol design

The HTTP contract between `wcpan-drive-server` and its clients. This package holds
the wire types (`NodeRecord`, change dicts) and their (de)serialization; the
routes and status codes below are implemented by the server's handlers. The
server's internal behavior is in `packages/server/SPEC.md`.

## An independent protocol

The server is an independent file service. It owns its node model, payloads, and
errors, and does not follow `wcpan.drive.client` types. There are two separate
adaptations, so that neither side's model has to track the other:

```text
Synology / filesystem
    -> backend adaptation -> server node model and HTTP API
    -> client-feed adaptation -> wcpan.drive.client model
```

Similar fields do not mean a shared runtime type. The server must not import
client packages.

## Node IDs

- Node IDs are opaque strings. Clients must not parse them or assume a spelling,
  and must percent-encode them in URL paths (any character may appear).
- An ID names one node for the life of the server database. A new database (after
  a schema change) issues new IDs, and clients detect this through stale cursors.
- The root has `parent_id: null`; it is the only such node. Its children are the
  configured mounts. The root and mounts cannot be renamed, moved, or deleted
  (403).

## Node payload

A node is a flat JSON object (`NodeRecordDict`): `id`, `parent_id`, `name`,
`is_directory`, `created_time`, `modified_time`, `changed_time` (seconds since the
epoch, UTC), `mime_type`, `hash`, `hash_algorithm`, `size`, `is_image`,
`is_video`, `width`, `height`.

`hash` and `hash_algorithm` are both empty for directories and for files without
a checksum. The backend defines the algorithm identifiers and clients interpret
them.

## Change feed

- `GET /api/v1/cursor` returns `{"cursor": ...}`, the current end of the change
  history.
- `GET /api/v1/root` returns the root node.
- `GET /api/v1/changes?cursor=C&max_size=N` returns
  `{"cursor", "has_more", "changes"}`. Each change is either
  `{"removed": false, "node": {...}}` or `{"removed": true, "node_id": ...}`.
  `max_size` is capped by the server (1000). Clients repeat the request with the
  returned cursor while `has_more` is true.

Cursors are opaque strings bound to one change history. `"0"` is the bootstrap
cursor and means "from the start of whatever history exists". Any other cursor is
valid only for the history that issued it.

A cursor is stale, and gets `410 Gone`, when:

- it comes from another history (the history was squashed, or it is from
  another database);
- it points past the current end (an older copy of the database was restored);
- it is a legacy integer cursor from servers before cursors had an epoch.

A malformed cursor gets 400. A client that gets 410 must discard everything it
derived from this server and replay from `"0"`.

Change semantics:

- Changes are in commit order. Within one page a node appears at most once,
  holding its latest state as of that page; a node can appear again on a later
  page.
- An upsert carries the node's full current record, not a delta.
- Every removed node gets its own removal. Clients must not assume a removal
  cascades to descendants, and do not need to cascade one themselves.
- A removal can be sent for a node the client never saw.

## Errors

| Status | Meaning |
| --- | --- |
| 400 | Malformed request, invalid name, or invalid cursor |
| 403 | Operation not allowed: root or mount nodes, moves between backends, operations the backend does not support |
| 404 | Unknown node or upload session; for create and update, also a node or folder the source no longer has, until the mirror records its removal |
| 409 | Name taken: the body is the existing node when the server can name it, otherwise plain text. For upload chunks, a 409 with an `Upload-Offset` header is an offset mismatch; for `DELETE` of an upload session, publishing is in progress |
| 410 | Stale change cursor; for uploads, the destination folder is gone |
| 416 | Download range outside the file |
| 422 | `{"error": "name_too_long", "message", "name"}` or `{"error": "invalid_media", "message"}`; retrying the same upload will not help |
| 503 | Server not ready, the target is not served by a running backend, the backend is busy or failed, or a transient upload failure (only the last sends `Retry-After`) |
| 507 | Permanent upload failure; for create and update, the source has no space left |

`DELETE /api/v1/nodes/{id}` removes a node and its subtree. The protocol has no
trash, restore, or permanent flag; whether the source keeps a recoverable copy is
up to the backend (see the backend specs).

A successful mutation (create, update, delete, upload) means the backend applied
it and the mirror has recorded it. The record it returns, `hash` included, is
the result to verify against. A request sent after the response sees the
change: the returned ID works at once as a node or parent, and lookups,
downloads, and the change feed reflect it. Node IDs are minted by the server, so
a client can address a new node only through the mirror; without this, chaining
a create into the next call would race the mirror. Clients still learn their own
changes from the change feed, like anyone else's. A change observed later by the
server, such as another client's edit, may already have replaced the result
by the time the client reads again.

Downloads (`GET /api/v1/nodes/{id}/download`, with optional `Range`):

- The response carries `Content-Length`, from the node's mirrored size or the
  range, and `Accept-Ranges: bytes`; a range response (206) also carries
  `Content-Range`. A range outside the file, or the empty suffix `bytes=-0`,
  gets 416 with `bytes */size`. A `Range` that is not one valid byte range
  (malformed, reversed, or several ranges) is ignored: the whole file is sent
  with 200. `Range` is also ignored alongside `If-Range`: the server sends no
  validator, so the condition never holds (RFC 9110 §13.1.5).
  `HEAD` returns the headers of a whole-file `GET` without reading the source;
  it ignores `Range`, since only `GET` has range semantics (RFC 9110 §14.2).
  Clients read range answers as RFC 9110 §14.4 and §15.3.7 require;
  `parse_content_range` in `lib` reads the header.
- A backend that cannot start the stream gets 503.
- Once the status is sent, a backend failure, or a stream shorter than
  `Content-Length`, aborts the connection. A client therefore never receives a
  complete-looking short body: it gets a transport error, or a body shorter
  than `Content-Length`, which HTTP clients report as an error.
- The length and the range's positions come from the mirror, and the backend
  serves the bytes at those positions. A source changed since its last
  observation can therefore send other bytes than the visible digest, or end
  short and fail as above; the client retries after the next sync.

## Uploads

- `POST /api/v1/nodes/{parent_id}?name=...` uploads in one request; the media
  fields can be passed as query parameters.
- `POST /api/v1/nodes/{parent_id}/uploads` with `{"name", "size", ...}` opens a
  resumable session. The response is 201 with `Location` and `Upload-Length`.
- `PATCH /api/v1/uploads/{id}` with `Upload-Offset` appends bytes. It returns 204
  with the new `Upload-Offset` while incomplete, and 201 with the node when the
  last byte is published.
- `HEAD /api/v1/uploads/{id}` reports `Upload-Offset` and `Upload-Length`.
- `DELETE /api/v1/uploads/{id}` cancels the session. It returns 409 once
  publishing has started; the client then looks the node up instead.

Upload sessions live in server memory and expire when idle, so a restart or
expiry loses them (404).
