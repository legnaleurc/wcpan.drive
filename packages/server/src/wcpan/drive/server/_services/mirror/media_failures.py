"""Records of files withheld after their media probe failed."""

from contextlib import closing

from wcpan.drive.sqlite import (
    read_only as _read_only,
)
from wcpan.drive.sqlite import (
    read_write as _read_write,
)

from ..._types import ProbeFailure
from .model import MediaFailure, now_ms


def mark_media_failure(dsn: str, key: int, source_key: str, kind: str) -> None:
    with _read_write(dsn) as con:
        con.execute(
            """
            INSERT INTO media_failures (instance, source_key, marked_at, kind)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instance, source_key) DO UPDATE SET
                marked_at = excluded.marked_at,
                kind = excluded.kind
            """,
            (key, source_key, now_ms(), kind),
        )


def clear_media_failure(dsn: str, key: int, source_key: str) -> None:
    with _read_write(dsn) as con:
        con.execute(
            "DELETE FROM media_failures WHERE instance = ? AND source_key = ?",
            (key, source_key),
        )


def prune_media_failures(dsn: str, key: int, before: int) -> int:
    with _read_write(dsn) as con:
        cur = con.execute(
            "DELETE FROM media_failures WHERE instance = ? AND marked_at < ?",
            (key, before),
        )
        return cur.rowcount


def get_media_failures(dsn: str, key: int) -> list[MediaFailure]:
    with _read_only(dsn) as con:
        with closing(
            con.execute(
                "SELECT source_key, kind FROM media_failures WHERE instance = ?"
                " ORDER BY source_key",
                (key,),
            )
        ) as cur:
            return [
                MediaFailure(str(row["source_key"]), _failure_kind(row["kind"]))
                for row in cur.fetchall()
            ]


def _failure_kind(value: object) -> ProbeFailure:
    try:
        return ProbeFailure(str(value))
    except ValueError:
        return ProbeFailure.INVALID
