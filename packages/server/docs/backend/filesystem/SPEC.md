# Local directory backend design

Backend-specific invariants. The generic contracts (public IDs, backend tables,
write queue) are in `packages/server/SPEC.md`.

## Object identity

A file's identity is evidence that two paths, or one path at two times, name the
same object. It is stored as an opaque string and never decoded.

- `<fsid>:h<type>:<hex>` is the Linux file handle from `name_to_handle_at()`,
  taken without following symlinks. A handle names the object, not the path, so
  it survives renames and is shared by hard links.
- `<fsid>:ino:<number>` is the fallback when the filesystem cannot export
  handles. An inode number can be reused after deletion, so it is weaker
  evidence.
- `fsid` is `statvfs().f_fsid`, which is derived from the filesystem UUID on ext4.
  A mount ID or device number is never used, because they can change across
  reboots.

Identity is evidence only. The backend does not open files by handle
(`open_by_handle_at()` needs `CAP_DAC_READ_SEARCH`); it opens by path and checks
that the path still names the expected identity, treating a mismatch as not found.

## Node IDs

Node IDs come from `NodeIdMap.mint()`. `fs_entries` maps each public node to its
identity and the stat signature its checksum was computed from.

- An entry keeps its node ID while it names the same object: the same name and
  identity under the same parent, or an identity whose previous path no longer
  names it (a rename or move).
- The mirror stays a tree, so each hard link is a separate node. The identity
  column is indexed, not unique. A new link to an object that is still in place
  gets its own node. Keeping an entry's ID through an ambiguous hard-link rename
  is not guaranteed.
- Clients cannot move across filesystems (`EXDEV` is reported as unavailable). A
  move made outside the server across filesystems appears as a removal plus a
  creation.

`fs_mounts` records each mount's source path and `fsid`. If the same source path
is later on a different filesystem, the mount is not scanned and its mirror is
kept, so an unmounted source is not taken for deleted files. An operator who
intends the change runs `backend <instance> rebind-mount <mount>`. Changing the
mount's configured source path is treated as a new source: the recorded `fsid` is
ignored and the new one is recorded on the next scan.

## Discovery

There is no durable event cursor, so every start is a full traversal.

- Each pass runs through the core's scan runner as one `ReconcileSession`. A full pass lists every folder; a scoped
  pass lists the folders reported as changed, plus new folders the mirror has not
  seen. Passes run one at a time, so two listings of one new entry cannot mint
  two IDs.
- Each directory is watched (inotify) before it is listed, so a change after a
  listing produces an event, as long as the watch was installed and the queue did
  not overflow. Watches map to directory node IDs, not paths,
  so they follow renames. Events only mark directories dirty; after a short
  settle delay, a scoped pass re-lists them. A dirty directory that is gone or
  replaced is swapped for its parent.
- Events during the startup scan are kept and handled after it.
- Queue overflow triggers a full rescan. Periodic full rescans
  (`rescan_interval`) cover directories that could not be watched and any other
  lost event. Without inotify, the backend relies on rescans alone.
- A mount whose source is missing or not a directory is not scanned, and its
  mirror is kept (see the `fsid` check above).
- Stat signatures and reused checksums are optimizations, not proof that a
  subtree is covered.

Writes (create, rename, move, delete, upload) run while scan passes are held off,
and are recorded in the mirror before passes resume, so a pass that lists the
result matches the same node. Renames and upload finalization use
`renameat2(RENAME_NOREPLACE)`, so they never replace an existing entry. Where
that is unsupported, they fall back to an existence check before `rename`, which
can race with another writer. A rename or delete first checks that the mirrored
path still names the known object. Uploads stage `.wcpan-upload-*` files in the
destination folder, and scans skip them. Unless the backend is read-only, a full
pass deletes staging files that no upload of this process holds and whose mtime
is older than twice the upload session TTL; the margin covers another server
sharing the folder.

## Checksums

Every published file has an `md5` checksum (lowercase hex of the whole file),
computed by the backend. A file is never published without one.

- A checksum is reused while the file's identity, size, and mtime are unchanged.
  ctime is not compared, since renames and metadata changes update it.
- Hashing checks size, mtime, and ctime before and after reading. If the file
  changed, or cannot be read, the observation is withheld and the last complete
  version stays visible until a later pass. A close-write event is only a hint
  to look again.
- Uploads are hashed as their bytes are written in order. A gap or a rewritten
  range falls back to hashing the staged file once it is complete.
- Media without complete client media info is probed in the staging file
  before the rename, outside the scan lock, so invalid media is never
  published and a transient failure leaves the session to be finished again.

Listings, hashing, and reads run on the backend's own bounded thread pool, kept
apart from the mirror's executor.

A media probe opens the file with `open_verified` against the observed
identity, and requires the observed size, modification time, and change time
when it opens and again after its last read; otherwise the result is discarded.
A rename changes the change time, so a file renamed mid-probe is probed again.

On Linux, MediaInfo reads that descriptor directly as `/dev/fd/N` (passed to
the subprocess) instead of over the server's loopback HTTP service. Opening
`/dev/fd/N` opens the same inode, so a file renamed or symlinked over the path
after `open_verified` is never the one probed, and MediaInfo's own seeks and
close leave the server's descriptor alone. The path name was rejected because
MediaInfo would resolve it again. Without the HTTP service the server sees none
of MediaInfo's reads: truncation and in-place rewrites are caught by the
recheck, but a local I/O error (`EIO`) that MediaInfo takes for the end of the
file goes undetected (the server SPEC's exception to failing on incomplete
reads). `/dev/fd/N` has no file extension, so formats MediaInfo detects only by
extension are not recognized. Uploads' staging files go over HTTP.

## Media failures

Media failure records are keyed by the source path, and IDs are minted in memory,
so a withheld file gets a fresh ID on each pass and none is stored. Each full scan
probes unpublished files again. A transient failure is also retried with
backoff by reading the file's folder again, which probes it as the scan does;
a scan that probes it first cancels the timer and renews the record, so pruning
never drops a record whose retry is pending. After a full scan and its
enrichment finish, records not renewed during it are pruned, which drops broken
files that were fixed, renamed, or deleted.
