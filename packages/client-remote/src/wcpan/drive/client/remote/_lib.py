from wcpan.drive.client.types import Node
from wcpan.drive.protocol.lib import (
    FOLDER_MIME_TYPE,
    ChangeDict,
    NodeRecordDict,
    RemovedChangeDict,
    UpsertChangeDict,
    guess_mime_type,
    node_record_from_dict,
    node_record_to_dict,
    utc_from_timestamp,
    utc_now,
)
from wcpan.drive.protocol.types import NodeRecord


__all__ = (
    "node_from_record",
    "RemovedChangeDict",
    "UpsertChangeDict",
    "guess_mime_type",
    "ChangeDict",
    "NodeRecordDict",
    "FOLDER_MIME_TYPE",
    "node_record_from_dict",
    "node_record_to_dict",
    "utc_from_timestamp",
    "utc_now",
)


def node_from_record(record: NodeRecord) -> Node:
    return Node(
        id=str(record.id),
        parent_id=str(record.parent_id) if record.parent_id is not None else None,
        name=record.name,
        is_directory=record.is_directory,
        is_trashed=False,
        created_time=utc_from_timestamp(record.created_time),
        modified_time=utc_from_timestamp(record.modified_time),
        changed_time=utc_from_timestamp(record.changed_time),
        mime_type=record.mime_type,
        hash=record.hash,
        size=record.size,
        is_image=record.is_image,
        is_video=record.is_video,
        width=record.width,
        height=record.height,
        ms_duration=record.ms_duration,
        private=None,
    )
