from datetime import datetime

from wcpan.drive.client.types import NodeQuery

from ._sql import SQL_JOIN_TABLES


# UNION (not UNION ALL) so a corrupted parent cycle cannot recurse forever.
_SQL_DESCENDANTS = """
WITH RECURSIVE descendants(id) AS (
    SELECT id FROM parents WHERE parent_id = ?
    UNION
    SELECT parents.id FROM parents
    JOIN descendants ON parents.parent_id = descendants.id
)
"""


type Param = str | int


def compile_query(query: NodeQuery, /) -> tuple[str, list[Param]]:
    """Compile *query* to SQL selecting SQL_JOIN_TABLES rows, and its params.

    Uses the REGEXP function when query.name_pattern is set.
    """
    prefix, conditions, params = compile_conditions(query)
    sql = prefix + SQL_JOIN_TABLES + conditions
    if query.limit is not None:
        sql += " LIMIT ?"
        params.append(query.limit)
    return sql + ";", params


def compile_summary(query: NodeQuery, /) -> tuple[str, list[Param]]:
    if query.limit is not None:
        raise ValueError("summaries do not support limit")
    prefix, conditions, params = compile_conditions(query)
    return (
        prefix + "SELECT COUNT(*) AS count, COALESCE(SUM(files.size), 0) AS total_size "
        "FROM nodes LEFT JOIN files ON nodes.id = files.id " + conditions + ";",
        params,
    )


def compile_conditions(query: NodeQuery, /) -> tuple[str, str, list[Param]]:
    prefix = ""
    where: list[str] = []
    params: list[Param] = []

    if query.ancestor_id is not None:
        prefix = _SQL_DESCENDANTS
        params.append(query.ancestor_id)
        where.append("nodes.id IN (SELECT id FROM descendants) AND nodes.id != ?")
        params.append(query.ancestor_id)

    if query.is_directory is not None:
        where.append(
            "files.id IS NULL" if query.is_directory else "files.id IS NOT NULL"
        )

    if query.is_trashed is not None:
        where.append("nodes.trashed = ?")
        params.append(query.is_trashed)

    # Directories have no files row, so these never match them.
    if query.mime_type_prefix is not None:
        where.append("substr(files.mime_type, 1, ?) = ?")
        params.extend((len(query.mime_type_prefix), query.mime_type_prefix))
    if query.size_min is not None:
        where.append("files.size >= ?")
        params.append(query.size_min)
    if query.size_max is not None:
        where.append("files.size <= ?")
        params.append(query.size_max)

    for column, after, before in (
        ("nodes.created_time", query.created_after, query.created_before),
        ("nodes.modified_time", query.modified_after, query.modified_before),
    ):
        if after is not None:
            where.append(f"{column} >= ?")
            params.append(_to_timestamp(after))
        if before is not None:
            where.append(f"{column} < ?")
            params.append(_to_timestamp(before))

    # Calls back into Python per row, so let the other conditions go first.
    if query.name_pattern is not None:
        where.append("nodes.name REGEXP ?")
        params.append(query.name_pattern)

    conditions = "WHERE " + " AND ".join(where) if where else ""
    return prefix, conditions, params


def _to_timestamp(time: datetime) -> int:
    # Same unit as inner_insert_node.
    return int(time.timestamp() * 1_000_000)
