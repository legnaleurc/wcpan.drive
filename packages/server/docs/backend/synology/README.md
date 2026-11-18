# Synology Drive backend

Synology Drive backend included in
[`wcpan-drive-server`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/README.md).

## Usage

Name it in the config as `type: wcpan.drive.server.backend.synology.create_backend`, or
generate a starting config with `wcpan.drive.server init --plugin synology`.

## Settings

```yaml
backends:
  nas:
    type: wcpan.drive.server.backend.synology.create_backend
    url: "https://nas.example.com:5001"
    username: "your-drive-user"
    password: "your-password"
    webhook_callback_url: "https://my-server.example.com"
    webhook_app_id: "1"
    # otp_code: "123456"
    # upload_tmp_dir: "/var/tmp/wcpan-drive"
mounts:
  photos:
    backend: nas
    source: "/volume1/photos"
```

`url`, `username`, `password`, `webhook_callback_url`, and `webhook_app_id`
are required; `otp_code` and `upload_tmp_dir` are optional. `webhook_app_id` is
an integer as a string. The server replaces the webhooks registered under it at
startup, so each server sharing the NAS needs its own. Mount sources are Synology
Drive paths. `upload_tmp_dir` is not a `server` setting; a config that sets
`server.upload_tmp_dir` is rejected.

Image and video dimensions are probed over the Drive API with ranged
downloads, so the server needs no NAS share mounted. MediaInfo decides how much
to read, so probing a large video can transfer most of it from the NAS. A file
is published once its probe succeeds.

## Behavior

Each instance:

- connects to Synology Drive through DSM's WebStation API using
  `wcpan-synology`
- registers a webhook at `WEBHOOK_CALLBACK_URL/api/v1/backends/NAME/webhook`, where
  `NAME` is the backend instance name
- keeps its mounts in sync through a startup scan, webhook events, and the
  `backend NAME backfill PATH` command
- reports MD4 file checksums (`hash_algorithm: md4`)
- soft-deletes: deleted files and folders go to the NAS recycle bin, which the
  server cannot restore from or empty
- spools resumable uploads to a temporary file under `upload_tmp_dir`
  (default: the system temporary directory), since Synology Drive takes a file
  in one request; the file is uploaded when the last chunk arrives. Direct
  uploads of images and videos without complete media info are spooled too,
  and probed before they reach the NAS (see below); other direct uploads
  stream straight through. Each server start removes spool
  directories that no running server holds, such as those left by a crash. If Synology Drive accepted the file but the
  server failed before recording it, finishing the session again reports the
  upload as a conflict with that file
- retries media files hidden after a failed probe at each start, and sooner
  after a transient failure

Node IDs are not Synology permanent links, but `backend NAME rebuild` gives
items their old node IDs back by matching permanent links.

## Commands

```bash
wcpan.drive.server --config server.yaml backend nas backfill /photos
wcpan.drive.server --config server.yaml backend nas pending
```

`backfill PATH` takes a virtual mirror path (`/` is the server root, then mount
names and folders) and reconciles this instance's mounts under it. It also
probes images and videos published without dimensions.
`pending` lists the media files this instance keeps hidden after a failed
probe, one per line: the Drive path, a tab, and why the probe failed
(`transient`, retried with backoff; `invalid`, unreadable media; `limit`, too
slow to probe within the deadline, retried at the next start). It logs in to
the Drive only when there is something to list.

`pending` and `backfill --dry-run` only read: they open the database without
writing any of its files and assign no node IDs, and they refuse a database
that needs a write first (see the server's `checkpoint` command).

## Design

Invariants and their reasons are in
[`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/docs/backend/synology/SPEC.md).
