# wcpan.drive

Asynchronous generic cloud drive library.

This package only provides the core functionality.
It need `SnapsnotService` and `FileService` implementation to work.

## Example Usage

```python
from wcpan.drive.core import create_drive
from wcpan.drive.core.types import (
    CreateFileService,
    CreateSnapshotService,
)
from wcpan.drive.core.lib import (
    download_file_to_local,
    upload_file_from_local,
)


# Assumes we already know how to create FileService and SnapshotService.
async def simple_demo(
    create_file_service: CreateFileService,
    create_snapshot_service: CreateSnapshotService,
):
    async with create_drive(
        file=create_file_service,
        snapshot=create_snapshot_service,
    ) as drive:
        # Check for authorization.
        if not await drive.is_authorized():
            # Start OAuth 2.0 process
            url = await drive.get_oauth_url()
            # ... The user visits the url ...
            # Get tokens from the user.
            token = ...
            # Finish OAuth 2.0 process.
            await drive.set_oauth_token(token)

        # It is important to keep the snapshot up-to-date.
        async for change in drive.sync():
            print(change)

        # Get the root node.
        root_node = await drive.get_root()

        # Get a node.
        node = await drive.get_node_by_path('/path/to/drive/file')

        # List children.
        children = await drive.get_children(root_node)

        # Make a directory.
        new_directory = await drive.create_directory('directory_name', root_node)

        # Download a file.
        await download_file_to_local(drive, node, '/tmp')

        # Upload a file.
        new_file = await upload_from_local(drive, root_node, '/path/to/local/file')

        # Traverse drive.
        async for root, directorys, files in drive.walk(root_node):
            print(root, directorys, files)
```
