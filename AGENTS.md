# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

This repository is a uv workspace for `wcpan.drive` client and server packages.

- Members live under `packages/*`, each with its own `pyproject.toml` and `src/`.
- Client code uses `wcpan.drive.client.*`; server code uses `wcpan.drive.server.*`.
- Shared HTTP wire types and helpers live in `packages/protocol`.
- Member tests live in their `tests/`; root `tests/` contains only cross-package tests (client against server). A member's tests import only that member and its declared dependencies.

Treat `README.md` and `server.example.yaml` as the source of truth for
user-facing usage, configuration examples, and documented backend
compatibility notes. Treat tests as the source of truth for expected behavior.

## Commands

```bash
make test       # compile + run all tests (unittest discovery)
make lint       # ruff format check + ruff lint + pyright
make format     # ruff format & fix in place
make coverage   # run tests with coverage report
```

Every `tests/` directory is a package named `tests`, so run tests from the
directory that holds it (`packages/<member>` or the repo root).

Run a single test file:

```bash
cd packages/<member> && uv run --all-packages python -m unittest tests/test_<module>.py
```

Run a specific test case:

```bash
cd packages/<member> && uv run --all-packages python -m unittest tests.test_<module>.TestClass.test_method
```

## Repo-specific conventions

- Use `unittest`, not `pytest`.
- Tests live in `<member>/tests/test_*.py`.
- Prefer `rg` for code search.
- Keep changes narrow and consistent with existing async `aiohttp` patterns.
- Follow the local signature style from `README.md`:
  keep subject inputs positional, and make service or infrastructure dependencies keyword-only when practical.
- Keep `AGENTS.md` focused on agent workflow; avoid repeating user-facing details already covered in `README.md`.

## Architecture Notes

Keep these as high-level orientation only:

- The server maintains a local SQLite-backed mirror of Synology Drive state.
- Startup scan, webhook processing, and explicit backfill all reconcile remote state into the local mirror.
- Path handling is split between Synology paths, client-facing virtual paths, and stored node identity.
- SQLite writes are serialized through background workers.
- Synology API access (client, types, errors) comes from the external `wcpan-synology` library (`../wcpan.synology` locally); `packages/server-synology/src/wcpan/drive/server/synology/` is the Synology backend. Change API behavior in that library, not here.
- `packages/server` must not import `wcpan.synology`, the Synology adapter, or client packages (`packages/server/tests/test_server_boundaries.py`).

If you need detailed implementation behavior, read the relevant module instead of extending this file with subsystem internals.

## Configuration Notes

The YAML config schema is split: generic settings in `packages/server/src/wcpan/drive/server/config.py` (parsed into `ServerConfig` in `server/types.py`) and the `synology` section in `packages/server-synology/src/wcpan/drive/server/synology/config.py`. The example config is `server.example.yaml`.

Do not duplicate config fields or semantics here; update `README.md`, `server.example.yaml`, and tests when behavior
changes.

## Maintaining This File

Keep `AGENTS.md` short and durable.

Include:

- commands agents should run,
- stable repository layout,
- testing and style conventions,
- pointers to canonical files.

Avoid:

- detailed subsystem inventories,
- line-by-line architecture explanations,
- transient implementation details that are better learned from the code.
