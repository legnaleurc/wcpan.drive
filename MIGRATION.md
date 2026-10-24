# Workspace migration

All nine renamed distributions begin at `0.1.0`. Existing installed packages are
not upgraded automatically to these new names. Replace dependencies and imports
using the table in README.md, and update application/provider configuration.
No old import aliases are provided. The default Synology webhook application ID
is now `wcpan-drive-server-synology`; explicitly configure the old app ID if
reusing an existing webhook registration. Constructor imports move from
`wcpan.drive.core` to `wcpan.drive.client.api`.

## Imported source revisions

Checked against GitHub `master` during workspace initialization:

| Previous repository | Imported source |
| --- | --- |
| `wcpan.drive.core` | Remote `91a72b5f1cc284721a0ac1c8a114658d65d7cd18` merged with local `a3b34e505414065958f55fc44f9b4e14505900c5` |
| `wcpan.drive.google` | Remote `32fc10f1b540b961cf2a0e00aebf83f906c5d3eb` |
| `wcpan.drive.crypt` | Local and remote `d7173d1ecae6bd714f927024ee0a2269cc560b5d` |
| `wcpan.drive.sqlite` | Local and remote `c520e1ee4682ea8edb353012d96d04efe30b0cce` |
| `wcpan.drive.cli` | Local and remote `50cd99cbe6dcc4a2c5e622cfd88fb58a9bc5c9b5` |

All source checkouts were clean. Core had two local commits absent from remote
master (timestamp renaming and a version bump); these were retained alongside
remote changes by merging in a temporary checkout. Other source repositories
were copied without modifying their worktrees or branches. This imports source
snapshots, not their Git histories.

The migration also corrects imported CLI mkdir argument ordering, OAuth refresh
error translation, and completion parsing edge cases found by strict typing.
Async context-manager annotations use AsyncGenerator. Existing member tests are
retained and run independently to avoid tests-package name collisions.

## Remaining architecture work

Per-file hash algorithms are implemented. The generic server defines backend
contracts, provides the `wcpan.drive.server` command, and no longer depends on
`wcpan-synology`; Synology configuration, discovery, and operations live in
`wcpan-drive-server-synology`, and local directories are served by
`wcpan-drive-server-filesystem`. Configuration version 3 names backend
instances and mounts; version 2 files still load as one Synology instance. The
Synology webhook route is now per instance
(`/api/v1/backends/<name>/webhook`, `synology` for version 2 files) and is
re-registered at startup, so only a reverse proxy that matches the exact old
path needs updating. Install `wcpan-drive-server` alongside the backend
packages: the backend packages no longer provide the command. Backend
maintenance commands moved under `backend NAME`, for example
`backend synology backfill /`. The Synology locator is private backend state
(schema 5, now 7); the wire format keeps only an empty `mutable_id`
placeholder. The remaining work is specified in PLAN.md.
