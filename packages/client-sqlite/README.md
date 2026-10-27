# wcpan.drive.client.sqlite

SQLite cache for wcpan.drive.

## Example Usage

```python
from contextlib import asynccontextmanager

from wcpan.drive.client.api import create_drive
from wcpan.drive.client.types import BackendFactory
from wcpan.drive.client.sqlite import create_cache


@asynccontextmanager
async def create_sqlite_cache():
    async with create_cache(dsn="/path/to/sqlite") as cache:
        yield cache


# Assumes we already know how to create Backend
async def simple_demo(create_backend: BackendFactory):
    async with create_drive(
        backend=create_backend,
        cache=create_sqlite_cache,
    ) as drive:
        ...
```
