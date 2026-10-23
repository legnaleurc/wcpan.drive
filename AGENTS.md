# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

This repository is a uv workspace for `wcpan.drive` client and server packages.

- Members live under `packages/*`, each with its own `pyproject.toml` and `src/`.
- Client code uses `wcpan.drive.client.*`; server code uses `wcpan.drive.server.*`.
- Shared HTTP wire types and helpers live in `packages/protocol`.
- Member tests live in their `tests/test_*.py`; root `tests/` holds workspace-wide and cross-package tests. A member's tests import only that member and its declared dependencies.

Treat each member's `README.md` as the source of truth for its
user-facing usage, configuration examples, and documented backend
compatibility notes; the root `README.md` is an index of the members plus
workspace development steps. Treat tests as the source of truth for expected behavior.

`docs/architecture.md` maps the members, their dependencies, and who owns
what. A member may have a `SPEC.md` with its design invariants and the reasons
for them. Read it before changing that member, and update it in the same change when
a documented contract changes.

## Commands

```bash
make test       # compile + run all tests (unittest discovery)
make lint       # ruff format check + ruff lint + pyright
make format     # ruff format & fix in place
make coverage   # run tests with coverage report
```

The `make` targets sync the environment; before running tests directly in a
fresh checkout, run `uv sync --all-packages --locked`.

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
- Prefer `rg` for code search.
- Keep changes narrow and consistent with existing async `aiohttp` patterns.
- Follow the signature style in the root `README.md`:
  keep subject inputs positional, and make service or infrastructure dependencies keyword-only when practical.
- Public and private modules follow `packages/client`: a member's public
  surface is a few role-named modules with `__all__` (`api` for entry points,
  `types`, `exceptions`, `lib`), or its root `__init__` for a small member or
  a plugin entry point. Everything else is `_`-prefixed at the member's root;
  modules inside a private package are not prefixed again. A public module that
  only re-exports is a facade: private modules never import it, so it cannot
  form a cycle. Source imports only other members' public modules; tests may
  also import their dependencies' private modules.
- When behavior changes, update the owning member's `README.md` and tests.
- Member READMEs are their PyPI descriptions: link other repository files with
  absolute URLs.
- Commit messages follow Conventional Commits
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `build:`, `chore:`).
  Scope a commit to the member it changes, named by its directory under
  `packages/` (`fix(server-synology):`); omit the scope when a commit spans
  several members or only touches workspace files. Mark breaking changes
  with `!` (`feat(client)!:`).
- Keep commit messages free of agent-specific content: no co-author
  trailers, generated-by lines, or tool names.

## Maintaining This File

Keep `AGENTS.md` short and durable.

Include:

- commands agents should run,
- stable repository layout,
- testing and style conventions,
- pointers to canonical files.

Avoid:

- detailed subsystem inventories, user-facing details covered by READMEs, or
  config fields and semantics;
- line-by-line architecture explanations;
- transient implementation details that are better learned from the code.
