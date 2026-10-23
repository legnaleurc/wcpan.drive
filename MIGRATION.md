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

The new workspace does not yet implement generic backend contracts or per-file
hash algorithms. The server still uses Synology-specific configuration and
orchestration, and running it requires `wcpan-drive-server-synology`. The remote
provider still uses MD4. The protocol still contains the existing mutable locator.
These are follow-up changes specified in PLAN.md, rather than completed features.
