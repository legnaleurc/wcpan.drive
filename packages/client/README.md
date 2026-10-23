# wcpan.drive.client

Asynchronous generic cloud drive library.

This package provides the core abstractions and a SQLite metadata cache. It
requires a `Backend` implementation to work. Providers:

- [`wcpan-drive-client-google`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-google/README.md): Google Drive
- [`wcpan-drive-client-feed`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-feed/README.md): a `wcpan-drive-server`
- [`wcpan-drive-client-crypt`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-crypt/README.md): encryption middleware over another provider

Load drives from a YAML file with
[`wcpan-drive-client-config`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client-config/README.md).

## Modules

Constructors are in `wcpan.drive.client.api`; types and exceptions have their
own modules (`wcpan.drive.client.types`, `wcpan.drive.client.exceptions`), and
file helpers are in `wcpan.drive.client.lib`.

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

## Cache

The SQLite metadata cache is built in; each source names its cache file with
`cache_path`. The cache file must be on a local filesystem that supports SQLite
WAL. The service API and the cache schema are at version 1; a cache at another
version is rejected, not migrated.

## Querying cached nodes

Use `drive.find_nodes()` to combine filters over the local cache. Await `.all()`
for matching nodes, or `.summarize()` for `NodeSummary(count, total_size)` without
loading node objects:

```python
summary = await (
    drive.find_nodes()
    .files_only()
    .include_trashed()
    .created(after=begin, before=end)
    .summarize()
)
print(summary.total_size)
```

Creation windows include `begin` and exclude `end`; times must be timezone-aware.
Queries exclude trashed nodes by default. Summaries count all matching nodes;
directories contribute zero bytes. No matches produce zero for both fields.
Use `.descendant_of(node)` to restrict a query to a directory's descendants,
excluding the directory itself. Across sources, summaries combine their totals.
Summaries reject `.limit()` because there is no defined ordering for a subset.

This replaces the SQLite-specific `get_uploaded_size` helper. The example sums
cached file sizes by creation time; it does not track upload traffic or files
that have been permanently deleted.

## Verifying files

Hash algorithms are per file. Nodes carry a digest in `hash` and its algorithm in
`hash_algorithm` (both empty for directories and for files without a checksum).
Each provider documents the algorithms it reports. Use
`Drive.get_hasher_factory(node)` to verify a file; it raises
`UnsupportedHashAlgorithmError` instead of guessing.

## Design

Invariants and their reasons are in
[`SPEC.md`](https://github.com/legnaleurc/wcpan.drive/blob/master/packages/client/SPEC.md).
