# wcpan.drive.client.cli

Command line tool for `wcpan.drive`.

This package needs plugins to actually work with a drive.

## Config

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config ...
```

Here is an example of the config file:

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

## Command Line Usage

Get the latest help:

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config -h
```

You need to authorize an user first.

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config auth
```

Then you should build local cache.
Many commands reliy on this cache to avoid making too many API requests.

Note that this is the **ONLY** command that will update the cache.
Which means after `upload`, `mkdir`, `remove`, `rename`, you need to run this
command to make the cache up-to-date.

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config sync
```

The `remove` command only put files to trash can, it does **NOT** permanently
remove **ANY** files.

However, some cloud services may clean up trashes after certain period.

Removing a folder will also remove all its descendants.

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config remove file1 file2 ...
```
