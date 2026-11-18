# Local directory backend

Local directory backend included in
[`wcpan-drive-server`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/README.md).

## Usage

Name it in the config as `type: wcpan.drive.server.backend.filesystem.create_backend`,
or generate a starting config with `wcpan.drive.server init --plugin filesystem`.

## Settings

Mount sources are absolute host directories. Settings, all optional:

```yaml
backends:
  disk:
    type: wcpan.drive.server.backend.filesystem.create_backend
    rescan_interval: 3600  # seconds between full rescans; default 3600
    watch: true            # follow changes with inotify; default true
    read_only: false       # reject every change through the server; default false
mounts:
  documents:
    backend: disk
    source: "/srv/documents"
```

## Behavior

- Discovery is a full scan at startup, then inotify: a changed directory is
  re-read about a second after its last change. Full rescans every
  `rescan_interval` seconds catch anything missed, such as writes through
  `mmap` or directories that could not be watched; if the kernel drops events,
  a full rescan runs immediately. Each watched directory uses one inotify
  watch; raise `fs.inotify.max_user_watches` for large trees. With
  `watch: false`, changes appear only after the next rescan.
- Regular files and directories are mirrored; symlinks and special files are
  skipped. Each directory entry is its own node, so hard links appear once per
  link.
- File checksums are MD5, computed by the server and reused while a file's
  size and modification time are unchanged (a rename does not rehash). A file that changes while being hashed
  keeps its previous version until the next scan.
- Node IDs follow the object, not the path, using Linux file handles (ext4 is
  the first supported filesystem; others fall back to inode numbers). Renames
  and moves keep their IDs; a replaced file gets a new one.
- Clients can create folders, rename, move, delete, and upload. Deletes are
  permanent; deleting a folder removes its contents. Renames, moves, and
  uploads never replace an existing entry: a taken name returns 409 with the
  existing node where possible. Invalid names (empty, `.`, `..`, or containing
  `/`) return 400, permission errors return 403, and a full disk returns 507.
  A node or folder removed from disk before a scan has seen it returns 404 (a
  delete of it succeeds). A node whose path now names a different file is not
  renamed or deleted (503); retry after the next scan. Moves across filesystems
  are not supported.
- Uploads are written to a hidden `.wcpan-upload-*` file in the destination
  folder and renamed into place when complete; interrupted uploads are removed,
  and these files are never mirrored. Resumable upload sessions write there
  directly. Images and videos without complete media info are probed in that
  file before it is renamed, so unreadable media is never published. If the
  folder moves during a session, finishing it returns 503
  until a scan has seen the move; if the folder is deleted, it returns 410.
  Full scans delete staging files that no upload of this server holds and that
  have not been written for two hours, such as those left by a crash; with
  `read_only: true` they are kept.
- With `read_only: true`, every change returns 403.
- Images and videos are probed where they are: on Linux MediaInfo reads the
  opened file directly, elsewhere through the server's loopback HTTP service.
  A probe of a file that changes meanwhile is discarded. Media files hidden after a failed probe are
  probed again on every full rescan, and sooner after a transient failure.
- A mount whose source is missing, unreadable, or now on a different
  filesystem (for example an unmounted disk) keeps its mirror instead of being
  emptied. If the filesystem change is intended, stop the server and run
  `backend NAME rebind-mount MOUNT`.

## Commands

```bash
wcpan.drive.server --config server.yaml backend disk rebind-mount documents
wcpan.drive.server --config server.yaml backend disk pending
```

`rebind-mount MOUNT` accepts a mount's source being on a different filesystem
than before (see above); if the recorded filesystem already matches, it says so
and records nothing. `pending` only reads: it opens the database without
writing any of its files, and refuses one that needs a write first (see the
server's `checkpoint` command). It lists the media files this instance keeps
hidden after a failed probe, one per line: the source path, a tab, and why the
probe failed (`transient`, retried with backoff; `invalid`, unreadable media;
`limit`, too slow to probe within the deadline, retried at the next full
rescan).

## Design

Invariants and their reasons are in
[`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/docs/backend/filesystem/SPEC.md).
