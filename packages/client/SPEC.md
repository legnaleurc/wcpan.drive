# wcpan-drive-client design

Invariants of the client framework that are not obvious from its types. Usage is
in `README.md`.

## Sources and IDs

A drive with several sources presents them under a virtual root and scopes each
node ID as `<source>:<id>`. Source names therefore must not contain `:`, which
both the framework and `client-config` reject. The ID after the first `:` is
passed to the backend unchanged, so a backend's IDs may contain `:`.

## Checksums

`Node` carries a flat `hash` (the digest string) and `hash_algorithm` (the
provider's identifier), both plain strings with empty meaning absent.

- A directory has neither. A file has both or neither; `Node` refuses anything
  else. A file without a checksum is valid, and its kind never depends on
  whether it has a hash.
- Algorithm choice is per file, and each provider defines its identifiers.
  `Drive.get_hasher_factory(node)` returns the factory for that node's algorithm,
  forwarded to the node's source with the unscoped node, and raises
  `UnsupportedHashAlgorithmError` for directories, files without a checksum, and
  unknown algorithms. Services never fall back to another algorithm, and there is
  no silent verification skip.
- Transfer helpers move bytes; callers verify checksums afterwards against the
  node they uploaded to or downloaded from, so no algorithm negotiation happens
  before an upload.
- `download_file_to_local` writes to `<name>.__tmp__` and renames it only once
  it holds `node.size` bytes. A stream that ends early raises and keeps the
  partial file, which the next call resumes; a short file never takes the final
  name, where a rerun would treat it as already downloaded.
- Hasher factories must be picklable (module-level callables or partials), since
  the CLI hashes in a process pool.

## Sync delivery

The cache is the source of truth, and changes yielded by `sync` are
notifications. Each page is applied to the cache, and its cursor saved, before
its changes are yielded, so each change is delivered at most once: a caller that
stops mid-page does not see the rest of that page again. With several sources,
each source runs ahead of the caller into a queue, so more can be skipped the
same way. Consumers keep no state built from changes; they read the cache, which
is never left inconsistent.

## Stale cursor recovery

A backend raises `StaleCursorError` when its change cursor no longer refers to
its history. `sync` for that source then rebuilds the source's cache:

- It runs under the source's lock, so no other sync of that source interleaves.
- `ChangeAction` has no reset event, so consumers learn of the reset as a
  removal for every cached node of the source except the root, followed by a
  full replay from the initial cursor. Downstream consumers stay correct without
  a new change type.
- The cache is emptied only after the source has yielded every removal, so a
  reset interrupted before then is redone by the next sync. After that, the
  replay rebuilds the cache, and removals the caller has not consumed (possible
  with several sources, see Sync delivery) are not announced again.
- Recovery is attempted once per `sync` call. The replay starts from the initial
  cursor, which a backend should never report as stale.
- The first stale cursor is handled inside the source, so other sources keep
  syncing. A second `StaleCursorError` during the replay propagates, and with
  several sources it cancels the others through the `TaskGroup`.
