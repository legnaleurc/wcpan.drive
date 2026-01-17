# wcpan.drive.sqlite

sqlite3 snapshot service for wcpan.drive.

## Example Usage

```python
from contextlib import asynccontextmanager

from wcpan.drive.core import create_drive
from wcpan.drive.core.types import FileService
from wcpan.drive.sqlite import create_service


@asynccontextmanager
async def create_sqlite_service():
    async with create_service(dsn="/path/to/sqlite") as snapshot:
        yield snapshot


# Assumes we already know how to create FileService
async def simple_demo(create_file_service: FileService):
    async with create_drive(
        file=create_file_service,
        snapshot=create_sqlite_service,
    ) as drive:
        ...
```
