# wcpan.drive.client.config

YAML configuration loader for wcpan.drive.

## Example Usage

```python
from pathlib import Path

from wcpan.drive.client.config import create_drive_from_config


async def main():
    async with create_drive_from_config(Path("/path/to/config.yaml")) as drive:
        ...
```

## Config

Example config file:

```yaml
version: 4
sources:
  # In multi-source mode (2+ sources), each name becomes a path prefix.
  - name: google
    # The first function creates the backend; the rest are middleware
    # applied in order. args and kwargs are optional.
    backend:
      - name: wcpan.drive.client.google.create_backend
        kwargs:
          client_secret: /path/to/client_secret.json
          oauth_token: /path/to/oauth_token.json
      - name: wcpan.drive.client.crypt.create_middleware
    cache:
      - name: wcpan.drive.client.sqlite.create_cache
        kwargs:
          dsn: /path/to/google.sqlite
```

Version 4 renamed the source keys `file` and `snapshot` to `backend` and
`cache`; version 3 files are rejected.

The whole file is validated before any factory is imported. Factory names are
imported and called as written, so a config file is trusted executable wiring;
load only files you control.
