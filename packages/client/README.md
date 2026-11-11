# wcpan.drive.client

Asynchronous generic cloud drive library.

This package provides the core abstractions and a SQLite metadata cache. It
requires a `Backend` implementation to work.

## Documentation

See the full documentation on [Read the Docs](https://wcpandrivecore.readthedocs.io/).

## Quick Example

```python
from wcpan.drive.client.api import create_drive
from wcpan.drive.client.lib import download_file_to_local, upload_file_from_local

async with create_drive(
    backend=create_my_backend,
    cache_path="/path/to/cache.sqlite",
) as drive:
    if not await drive.is_authenticated():
        await drive.authenticate()

    async for change in drive.sync():
        print(change)

    root = await drive.get_root()
    node = await upload_file_from_local(drive, local_path, root)
    await download_file_to_local(drive, node, dest_dir)
```

When a backend reports that its change cursor is stale (`StaleCursorError`, for
example after the server's change history was squashed), `sync` rebuilds that
source's cache: it yields a removal for every cached node of the source, then
replays the source from its initial cursor. Other sources are not affected.
