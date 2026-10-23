# wcpan-drive-client-feed

`Backend` provider for
[`wcpan-drive-client`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client/README.md)
that talks to a running
[`wcpan-drive-server`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/server/README.md).

## Installation

```bash
pip install wcpan-drive-client-feed
```

## Example

```python
from wcpan.drive.client.feed import create_backend

async with create_backend(server_url="http://localhost:8080") as backend:
    root = await backend.get_root()
    print(f"Root: {root}")
```

Use it as a drive source like any other provider:

```python
from functools import partial

from wcpan.drive.client.api import create_drive
from wcpan.drive.client.feed import create_backend

async with create_drive(
    backend=partial(create_backend, server_url="http://localhost:8080"),
    cache_path="/path/to/cache.sqlite",
) as drive:
    ...
```

In a [`wcpan-drive-client-config`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-config/README.md)
file, the factory is `wcpan.drive.client.feed.create_backend` with the
`server_url` setting; `init -p feed` generates it.

## Behavior

- File checksums are whatever the server's backend reports; this provider
  accepts `md4`, `md5`, `sha1`, and `sha256`.
- What deleting, trashing, and renaming do is up to the server's backend; see
  that backend's README.
- When the server answers a change cursor with `410 Gone` (for example after
  its history was squashed), this provider raises `StaleCursorError` and the
  client rebuilds the source's cache.

## Design

Invariants and their reasons are in
[`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-feed/SPEC.md);
the HTTP contract is in the
[protocol spec](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/protocol/SPEC.md).
