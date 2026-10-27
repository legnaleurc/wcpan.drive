"""wcpan.drive.client - Cloud drive abstraction layer for Python.

wcpan.drive.client provides a unified interface for interacting with cloud storage
services. It abstracts away service-specific details behind a consistent API,
making it easy to build cloud-agnostic applications.

Key Features:
    - Unified Drive interface for all cloud storage operations
    - Local metadata caching for fast path lookups
    - Resumable file uploads and downloads
    - Change synchronization with cursor-based tracking
    - Type-safe async/await API
    - Extensible through service implementations

Architecture:
    The library follows a three-layer architecture:

    1. **Drive (Public API)**: High-level interface for user applications
       - Path-based and ID-based node access
       - File upload/download operations
       - Directory management and traversal
       - Change synchronization

    2. **Services (Backend)**: Implementation layer
       - Backend: Cloud storage API operations
       - Cache: Local metadata caching
       - Middleware: Cross-cutting concerns (logging, caching, etc.)

    3. **Types**: Core data structures
       - Node: Immutable file/directory representation
       - MediaInfo: Image/video metadata
       - ChangeAction: Sync event types

Quick Start:
    Basic usage pattern::

        >>> from wcpan.drive.client.api import create_single_drive
        >>> from pathlib import Path, PurePath
        >>>
        >>> # Create drive with your service implementations
        >>> async with create_single_drive(
        ...     backend=create_my_backend,
        ...     cache=create_my_cache,
        ... ) as drive:
        ...     # Authenticate
        ...     await drive.authenticate()
        ...
        ...     # Sync to get latest changes
        ...     async for change in drive.sync():
        ...         print(f"Change: {change}")
        ...
        ...     # Navigate filesystem
        ...     root = await drive.get_root()
        ...     children = await drive.get_children(root)
        ...     for child in children:
        ...         print(f"{child.name}: {child.size} bytes")
        ...
        ...     # Upload a file
        ...     from wcpan.drive.client.lib import upload_file_from_local
        ...     node = await upload_file_from_local(
        ...         drive,
        ...         Path("/local/document.pdf"),
        ...         root,
        ...     )
        ...
        ...     # Download a file
        ...     from wcpan.drive.client.lib import download_file_to_local
        ...     local_path = await download_file_to_local(
        ...         drive,
        ...         node,
        ...         Path("/downloads"),
        ...     )

Implementing Services:
    To use wcpan.drive.client with a specific cloud provider, implement
    Backend and Cache::

        >>> from wcpan.drive.client.types import Backend, Cache
        >>> from contextlib import asynccontextmanager
        >>>
        >>> class MyCloudBackend(Backend):
        ...     @property
        ...     def api_version(self) -> int:
        ...         return 6
        ...
        ...     async def get_root(self) -> Node:
        ...         # Implement cloud API calls
        ...         pass
        ...
        ...     # ... implement other abstract methods ...
        >>>
        >>> @asynccontextmanager
        >>> async def create_my_service():
        ...     service = MyCloudBackend(credentials)
        ...     try:
        ...         yield service
        ...     finally:
        ...         await service.cleanup()

Available Imports:
    - create_drive: Entry point for single-source Drive instances
    - create_multi_drive: Entry point for multi-source Drive instances

    Types such as Drive and SourceConfig are available in wcpan.drive.client.types.

For detailed API documentation, see:
    - Drive interface: wcpan.drive.client.types.Drive
    - Utility functions: wcpan.drive.client.lib
    - Data types: wcpan.drive.client.types (Node, MediaInfo)
    - Exceptions: wcpan.drive.client.exceptions
"""

from importlib.metadata import version

from ._drive import compose_service, create_drive, create_multi_drive


__version__ = version("wcpan-drive-client")
__all__ = ("compose_service", "create_drive", "create_multi_drive")
