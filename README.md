# wcpan.drive

Asynchronous generic cloud drive library with multiple implementations.

## Monorepo Structure

This repository is a uv workspace monorepo containing all wcpan.drive packages:

- **[core](./packages/core/)** - Core library with generic drive abstraction
- **[cli](./packages/cli/)** - Command-line interface
- **[crypt](./packages/crypt/)** - Encryption middleware
- **[google](./packages/google/)** - Google Drive implementation
- **[sqlite](./packages/sqlite/)** - SQLite snapshot service

## Development Setup

### Prerequisites

- Python 3.12 or later
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd wcpan.drive

# Install all dependencies
make install
```

### Common Tasks

```bash
# Format code
make format

# Lint code
make lint

# Run all tests
make test

# Run specific package tests
make test-core
make test-cli

# Build all packages
make build
```

## Package Documentation

Each package has its own README with specific usage instructions:

- [core README](./packages/core/README.md)
- [cli README](./packages/cli/README.md)
- [crypt README](./packages/crypt/README.md)
- [google README](./packages/google/README.md)
- [sqlite README](./packages/sqlite/README.md)

## Architecture

### Dependency Graph

```
wcpan.drive.core (foundation)
    ├── wcpan.drive.cli (command-line tool)
    ├── wcpan.drive.crypt (encryption middleware)
    ├── wcpan.drive.google (Google Drive implementation)
    └── wcpan.drive.sqlite (SQLite snapshot service)
```

## License

MIT License - see [LICENSE.txt](./LICENSE.txt)
