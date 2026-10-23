# wcpan-drive-client-crypt design

## Checksums

Stored content is encrypted, so the backend's checksum describes the encrypted
bytes. `get_hasher_factory(node)` asks the wrapped backend for the node's factory
and wraps it so every update encrypts the data before hashing. A caller hashing
plaintext therefore gets a digest comparable with the backend's.

The node is always forwarded to the wrapped backend. A global algorithm lookup
would bypass this wrapping.
