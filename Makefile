UV := uv
RUN := $(UV) run --all-packages
MEMBER_CONFIGS := $(wildcard packages/*/pyproject.toml)

.PHONY: all venv format lint test coverage build publish

all: venv

venv:
	$(UV) sync --all-packages --locked

format: venv
	$(RUN) ruff format packages tests scripts
	$(RUN) ruff check --fix packages tests scripts

lint: venv
	$(RUN) ruff format --check packages tests scripts
	$(RUN) ruff check packages tests scripts
	$(RUN) pyright

test: venv
	$(RUN) python -m compileall -q packages scripts
	$(RUN) python scripts/test_workspace.py

coverage: venv
	$(RUN) coverage erase
	COVERAGE_PROCESS_START=$(CURDIR)/.coveragerc $(RUN) coverage run scripts/test_workspace.py
	$(RUN) coverage combine
	$(RUN) coverage report -m

build:
	$(UV) build --all-packages --no-sources --clear

publish:
	$(UV) publish
