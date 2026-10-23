from asyncio import Event

from aiohttp.web import AppKey

from ._lib.write_queue import (
    WriteQueue,
)
from ._services.mirror.store import (
    MirrorStore,
)
from ._services.off_main import OffMainService
from ._services.operations import NodeOperations
from ._services.upload.service import UploadService
from ._types import (
    ServerConfig,
)


CONFIG_KEY = AppKey[ServerConfig]("server_config")
OFF_MAIN_KEY = AppKey[OffMainService]("off_main")
WRITE_QUEUE_KEY = AppKey[WriteQueue]("write_queue")
MIRROR_KEY = AppKey[MirrorStore]("mirror")
# Set once every backend is ready. The app freezes after startup, so readiness
# flips by setting this event rather than by replacing the key.
READY_KEY = AppKey[Event]("ready")
NODE_OPERATIONS_KEY = AppKey[NodeOperations]("node_operations")
UPLOAD_SERVICE_KEY = AppKey[UploadService]("upload_service")
