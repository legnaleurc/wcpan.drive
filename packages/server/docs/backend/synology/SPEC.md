# Synology Drive backend design

Backend-specific invariants. The generic contracts (public IDs, backend tables,
write queue) are in `packages/server/SPEC.md`.

## Node IDs

The native ID is the NAS permanent link, which is stable for the object's life on
the NAS. Because of that, `backend NAME rebuild` restores nodes under their old
public IDs.

- Converting API info to a record calls `NodeIdMap.public(permanent_link)`.
  Parents resolve to public IDs through locators or mount roots.
- A request names its parent by public ID, which resolves to a NAS `file_id`
  through the locator, since the NAS API takes only a path or a `file_id` as a
  parent. The locator commits with the node, and the core answers a mutation
  only after that commit (server SPEC, Mutation visibility), so a folder
  returned by a create can be used as a parent at once. A parent without a
  locator is an unknown node (404).
- Calls that send a node to the NAS use `native_of`.
- Webhook deletions use `lookup`; an unknown permanent link has nothing to
  delete.
- Comparisons inside the backend (the move check, backfill against mirror IDs)
  stay correct because both sides are public IDs.

## Discovery

`sync_id`, `max_id`, watermarks, and count checks stay inside this backend.

- The startup scan is an incremental breadth-first pass through the core's scan
  runner, in one `ReconcileSession` over all mounts, one mount at a time. A folder whose `max_id` is not above the
  mount's checkpoint is skipped (its subtree preserved), unless the mirror has no
  children for it or the NAS reports a different child count. A count is only a
  reason to enter a folder; a matching count is not proof that nothing changed.
- Each mount's checkpoint is the watermark read before its scan, so changes
  during the scan are seen again next time. A mount with any folder that could
  not be listed keeps its previous checkpoint, so the next scan lists that folder
  again instead of skipping it by `max_id`. A failed count check only preserves
  the skipped folder: its `max_id` is at or below the old checkpoint either way,
  so the next scan repeats the check whether or not the checkpoint advanced.
- Checkpoints are written after the session's removals and the scan's enrichment
  have landed. Media failure retries queued just before are not waited for;
  their records persist, so a crash only repeats the retry.
- Webhooks are queued while the startup scan runs, so the scan sees a stable
  mirror, and the server becomes ready once that queue is consumed. File events are debounced, so their delayed
  updates may still land after readiness. A removal of an unknown permanent link
  is ignored.
- Removal candidates at the end of the scan are fetched by permanent link, so a
  node moved during the scan is recorded as moved, not removed. Only a missing
  node (`SynologyNotFoundError`) or an unresolvable parent counts as absence;
  any other failure keeps the candidate.
- `backfill` is an offline command that reconciles one virtual path in a
  `ReconcileSession`, comparing every field rather than trusting checkpoints.
  It also probes published images and videos without complete dimensions.

Failures are let to crash. A NAS call that fails while processing a webhook
(fetching the file, listing a moved folder, a debounced update) stops the server
instead of skipping the event, since it means the NAS or its API is unstable.
The restart's initial scan then repairs the mirror: the checkpoint advances only
during that scan, so it covers every change since the last start. Webhook
registration failures likewise stop startup. Events the NAS never delivers
cannot be detected, since there is no webhook health check; the next start's
scan, or a manual `backfill`, picks those changes up.

## Errors

Drive API errors are translated from `wcpan-synology`'s
semantic exceptions, never from raw codes. `get_file()` raises
`SynologyNotFoundError` for a missing node; lookups where absence is an answer
treat that exception as absence. A missing child is the expected answer of a
conflict lookup, not an error; a missing parent (`SynologyDirectoryNotFoundError`) is.
Drive may report a missing parent as a missing node instead, in which case the
upload itself fails as parent gone.

| Exception | Operations | Uploads |
| --- | --- | --- |
| `SynologyConflictError` | 409 | 409 with the existing node |
| `SynologyNotFoundError` | 404; a delete counts as done | 410 (parent gone) |
| `SynologyPermissionError` | 403 | 403 |
| `SynologyUnsupportedError` | 403 | 507 |
| `SynologyInvalidNameError` | 400 | 400 |
| `SynologyNameTooLongError` | 400 | 422 `name_too_long` |
| `SynologyPathTooLongError`, `SynologyInvalidOperationError` | 400 | 507 |
| `SynologyBusyError`, `SynologyNetworkError` | 503 | 503 with `Retry-After` |
| `SynologyStorageFullError`, `SynologyQuotaExceededError` | 507 | 507 |
| Too large, locked, authentication, protocol, unknown codes | 503 | 507 |

## Deletes

Deletes are soft. The NAS delete API (`SYNO.SynologyDrive.Files` `delete`) has a
`permanent` flag that defaults to `false`; `wcpan-synology`'s `delete()` never
sends it, so a deleted item goes to the NAS recycle bin. The recycle bin is not
exposed through the API, so the server cannot restore or purge it, and the
protocol has no trash or permanent flag to request a hard delete
(`packages/protocol/SPEC.md`).

## State tables

- `synology_locators` maps a public node ID to the NAS `file_id`, with an index on
  `(instance, file_id)` for webhook reverse lookup. Two NAS instances can report
  the same `file_id`, which the instance key keeps apart.
- `synology_mounts` holds each mount's discovery checkpoint (`max_id`) together
  with the source path it was taken from. A changed source path resets the
  checkpoint.

## Checksums and media

- The checksum is the NAS's `md4` hash, reported with the rest of the file's
  metadata. A file the NAS reports without a hash is published with both hash
  fields empty. Because the checksum arrives with the metadata, the only
  incomplete window is media probing.
- Media is probed over the Drive API: each stream is one ranged download,
  accepted only as a `206` whose `Content-Range` names exactly the requested
  range of a file of the observed size, with a body of exactly that length.
  Anything else (a full `200`, or a `200` JSON error such as an expired
  session) fails the read. After MediaInfo exits the file is looked up again, and a size,
  modification time, or MD4 hash other than the observation's discards the
  result. A change the API does not report yet goes unnoticed.
- A node download is one ranged Drive API download of the positions the core
  resolved against the mirrored size, accepted only as a `206` whose
  `Content-Range` names those positions. The total may differ from the mirrored
  size, since a changed file is still served under the Publication rule; a full
  `200` or a JSON error fails the download before its status is sent.
- Media failure records are keyed by the permanent link, whose public ID is
  minted on conversion anyway and is stable.
- Scans resume from checkpoints and would not see an unchanged failed file again,
  so after the startup scan every record is re-observed and probed, whatever
  its kind. While serving, a transient failure is retried with backoff by
  looking the file up by its permanent link. A record clears when its file is
  applied or removed, or when a retry finds it gone.
