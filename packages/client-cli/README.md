# wcpan.drive.client.cli

Command line tool for `wcpan.drive`.

This package needs plugins to actually work with a drive.

## Config

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config ...
```

The config file format is documented in `wcpan-drive-client-config`.
Generate a starting config for installed plugins without `-c`:

```sh
python3 -m wcpan.drive.client.cli init -p google > /path/to/config
```

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

The `remove` command soft-deletes (moves to the trash) by default. This is best
effort: a backend without a trash may delete permanently. `--purge` asks for a
permanent delete and `--restore` restores from the trash, where the backend
supports them. See your backend's documentation for what it does.

Removing a folder will also remove all its descendants.

```sh
python3 -m wcpan.drive.client.cli -c /path/to/config remove file1 file2 ...
```

`usage` (`du`) sums file sizes from the local cache, so it reflects the last
`sync`.
