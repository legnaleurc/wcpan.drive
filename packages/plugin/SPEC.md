# wcpan-drive-plugin design

Shared by client providers and server backends. Usage is in `README.md`.

## Settings are the factory signature

A plugin's settings are its factory's keyword-only parameters. The signature
defines the setting names, types, defaults, and help, so generated configs and
the type checks follow the factory. A factory can still validate meaning beyond
types itself, as server backends do in their `_config.py`.

- A parameter without a default is required. `Annotated[T, "help"]` gives the
  help text rendered into generated configs.
- `check_settings` rejects unknown keys (unless the factory takes `**kwargs`),
  missing required keys, and mistyped values. It checks `str`, `int`, `float`,
  `bool`, `None`, unions, lists, and mappings, recursively. Other annotations are
  passed through unchecked. `bool` is never accepted as `int` or `float`; an
  `int` given for a `float` becomes a `float`.
- Checking is done by the caller. The server checks every backend's settings
  before entering it, and `wcpan-drive-client-config` checks every factory's
  `kwargs` before calling any of them.

## Discovery

Plugins are entry points in a group (`wcpan.drive.client.backend`,
`wcpan.drive.server.backend`). `find_plugin` accepts an entry point name, the
factory's module, or its dotted path, and refuses a selector that matches none or
several. Configs always record the dotted path (`factory_path`), so they do not
depend on entry point names.
