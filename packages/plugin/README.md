# wcpan-drive-plugin

Plugin discovery and factory settings for wcpan.drive clients and servers.

A plugin is a factory function registered as a package entry point. Its
keyword-only parameters are its settings: their names are the config keys,
their annotations are the accepted types, and an `Annotated` string is the help
text shown in generated configs.

```toml
[project.entry-points."wcpan.drive.client.backend"]
google = "wcpan.drive.client.google:create_backend"
```

```python
async def create_backend(
    *,
    client_secret: Annotated[str, "Path to the OAuth client secret JSON."],
    oauth_token: Annotated[str, "Path to the OAuth token file."],
): ...
```

- `find_plugin` selects one installed plugin by entry point name, module, or
  dotted factory path.
- `get_settings` reads a factory's settings.
- `check_settings` validates config values against them.
- `render_settings` writes them as commented YAML lines.
