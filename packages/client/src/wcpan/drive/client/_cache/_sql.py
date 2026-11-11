from typing import Literal, TypedDict


class JoinedDict(TypedDict):
    id: str
    name: str
    trashed: Literal[0, 1]
    created_time: int
    modified_time: int
    changed_time: int
    parent_id: str | None
    file_id: str | None
    size: int | None
    hash: str | None
    hash_algorithm: str | None
    mime_type: str | None
    width: int | None
    height: int | None
    ms_duration: int | None
    extra: str | None


SQL_JOIN_TABLES = """
SELECT
    nodes.id AS id,
    nodes.name AS name,
    nodes.trashed AS trashed,
    nodes.created_time AS created_time,
    nodes.modified_time AS modified_time,
    nodes.changed_time AS changed_time,
    parents.parent_id AS parent_id,
    files.id AS file_id,
    files.mime_type AS mime_type,
    files.hash AS hash,
    files.hash_algorithm AS hash_algorithm,
    files.size AS size,
    images.width AS width,
    images.height AS height,
    audios.ms_duration AS ms_duration,
    extras.json AS extra
FROM nodes
LEFT JOIN parents ON nodes.id = parents.id
LEFT JOIN files ON nodes.id = files.id
LEFT JOIN images ON nodes.id = images.id
LEFT JOIN audios ON nodes.id = audios.id
LEFT JOIN extras ON nodes.id = extras.id
"""
SQL_SELECT_NODE_BY_ID = SQL_JOIN_TABLES + "WHERE nodes.id = ?;"
SQL_SELECT_CHILD_BY_NAME = (
    SQL_JOIN_TABLES + "WHERE parents.parent_id = ? AND nodes.name = ?;"
)
SQL_SELECT_CHILDREN_BY_ID = SQL_JOIN_TABLES + "WHERE parents.parent_id = ?;"
SQL_SELECT_TRASHED_NODES = SQL_JOIN_TABLES + "WHERE nodes.trashed = ?;"
