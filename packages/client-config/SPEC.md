# wcpan-drive-client-config design

Loads the drive configuration (version 1) into `SourceConfig`s. Usage and the
schema are in `README.md`.

- The whole file is validated before any factory module is imported, so a
  malformed file has no side effects.
- A config file is trusted executable wiring. Factory names are imported and
  called as written, and `safe_load` does not make those imports safe.
- The core's typed `SourceConfig` API is the public contract, not this YAML
  schema; other applications can build `SourceConfig`s directly.
- Source names must be unique, non-empty, and free of `:` (see
  `packages/client/SPEC.md`).
- Each source's chain is a backend factory followed by middleware, composed in
  order. Each factory's `kwargs` are checked against its settings with
  `check_settings` (see `packages/plugin/SPEC.md`) once it is imported, and
  every factory in the file is checked before any is called. A bad setting is a
  `ValueError` naming the source and chain position. Positional `args` are
  passed as written.
- Only the current version is accepted; other versions are rejected, not
  converted.
