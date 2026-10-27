from aiohttp.web import AppKey

from .services.mirror import MirrorStore
from .services.off_main import OffMainService
from .services.operations import NodeOperations
from .services.sync import NodeSyncService
from .services.upload import UploadService
from .types import ServerConfig, WriteQueue


CONFIG_KEY = AppKey[ServerConfig]("server_config")
OFF_MAIN_KEY = AppKey[OffMainService]("off_main")
WRITE_QUEUE_KEY = AppKey[WriteQueue]("write_queue")
MIRROR_KEY = AppKey[MirrorStore]("mirror")
READY_KEY = AppKey[bool]("ready")
CHANGE_SERVICE_KEY = AppKey[NodeSyncService]("change_service")
NODE_OPERATIONS_KEY = AppKey[NodeOperations]("node_operations")
UPLOAD_SERVICE_KEY = AppKey[UploadService]("upload_service")
