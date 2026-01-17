.PHONY: help install format lint test test-core test-cli test-crypt test-google test-sqlite clean build

help:
	@echo "wcpan.drive monorepo commands:"
	@echo "  make install       - Install all dependencies"
	@echo "  make format        - Format all code with ruff"
	@echo "  make lint          - Lint all code with ruff"
	@echo "  make test          - Run all tests"
	@echo "  make test-core     - Run core package tests"
	@echo "  make clean         - Clean build artifacts"
	@echo "  make build         - Build all packages"

install:
	uv sync --all-packages

format:
	uv run ruff check --fix .
	uv run ruff format .

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	@echo "Running all tests..."
	@cd packages/core && uv run --package wcpan-drive-core python -m unittest discover -s tests
	@cd packages/cli && uv run --package wcpan-drive-cli python -m unittest discover -s tests
	@cd packages/crypt && uv run --package wcpan-drive-crypt python -m unittest discover -s tests
	@cd packages/google && uv run --package wcpan-drive-google python -m unittest discover -s tests
	@cd packages/sqlite && uv run --package wcpan-drive-sqlite python -m unittest discover -s tests

test-core:
	cd packages/core && uv run --package wcpan-drive-core python -m unittest discover -s tests

test-cli:
	cd packages/cli && uv run --package wcpan-drive-cli python -m unittest discover -s tests

test-crypt:
	cd packages/crypt && uv run --package wcpan-drive-crypt python -m unittest discover -s tests

test-google:
	cd packages/google && uv run --package wcpan-drive-google python -m unittest discover -s tests

test-sqlite:
	cd packages/sqlite && uv run --package wcpan-drive-sqlite python -m unittest discover -s tests

clean:
	rm -rf build dist **/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

build:
	@echo "Building all packages..."
	cd packages/core && uv build
	cd packages/cli && uv build
	cd packages/crypt && uv build
	cd packages/google && uv build
	cd packages/sqlite && uv build
