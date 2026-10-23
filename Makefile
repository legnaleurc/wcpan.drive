UV := uv
RUN := $(UV) run --all-packages
MEMBER_CONFIGS := $(wildcard packages/*/pyproject.toml)
TEST_SUITES := . $(wildcard packages/*)

.PHONY: all venv format lint test coverage build publish

all: venv

venv:
	$(UV) sync --all-packages --locked

format: venv
	$(RUN) ruff format packages tests
	$(RUN) ruff check --fix packages tests

lint: venv
	$(RUN) ruff format --check packages tests
	$(RUN) ruff check packages tests
	$(RUN) pyright

test: venv
	$(RUN) python -m compileall -q packages
	@set -e; for suite in $(TEST_SUITES); do \
		if [ -d "$$suite/tests" ]; then \
			echo "Testing $$suite"; \
			(cd "$$suite" && $(RUN) python -m unittest discover -s tests -t .); \
		fi; \
	done

coverage: venv
	$(RUN) coverage erase
	@set -e; for suite in $(TEST_SUITES); do \
		if [ -d "$$suite/tests" ]; then \
			echo "Testing $$suite"; \
			(cd "$$suite" && COVERAGE_FILE=$(CURDIR)/.coverage $(RUN) coverage run --rcfile=$(CURDIR)/.coveragerc -m unittest discover -s tests -t .); \
		fi; \
	done
	$(RUN) coverage combine
	$(RUN) coverage report -m

build:
	$(UV) build --all-packages --no-sources --clear

publish:
	$(UV) publish
