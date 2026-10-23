from aiohttp.web import AppKey
from wcpan.synology import SynologyClient

from .lib.mounts import MountRegistry
from .services.off_main import OffMainService
from .services.paths import SynologyPathService
from .services.storage import StorageService
from .services.sync import NodeSyncService
from .services.upload import UploadService
from .types import ServerConfig, WriteQueue
from .workers import WebhookQueue


CONFIG_KEY = AppKey[ServerConfig]("server_config")
MOUNT_REGISTRY_KEY = AppKey[MountRegistry]("mount_registry")
OFF_MAIN_KEY = AppKey[OffMainService]("off_main")
WRITE_QUEUE_KEY = AppKey[WriteQueue]("write_queue")
STORAGE_KEY = AppKey[StorageService]("storage")
SYNOLOGY_DRIVE_API_KEY = AppKey[SynologyClient]("synology_drive_api")
READY_KEY = AppKey[bool]("ready")
SYNOLOGY_PATH_KEY = AppKey[SynologyPathService]("synology_path")
CHANGE_SERVICE_KEY = AppKey[NodeSyncService]("change_service")
UPLOAD_SERVICE_KEY = AppKey[UploadService]("upload_service")
WEBHOOK_QUEUE_KEY = AppKey[WebhookQueue]("webhook_queue")
