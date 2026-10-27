# wcpan.drive.client

Asynchronous generic cloud drive library.

This package provides the core abstractions only. It requires a
`Cache` and `Backend` implementation to work.

## Documentation

See the full documentation on [Read the Docs](https://wcpandrivecore.readthedocs.io/).

## Quick Example

```python
from wcpan.drive.client.api import create_drive
from wcpan.drive.client.lib import download_file_to_local, upload_file_from_local

async with create_drive(
    backend=create_my_backend,
    cache=create_my_cache,
) as drive:
    if not await drive.is_authenticated():
        await drive.authenticate()

    async for change in drive.sync():
        print(change)

    root = await drive.get_root()
    node = await upload_file_from_local(drive, local_path, root)
    await download_file_to_local(drive, node, dest_dir)
```
