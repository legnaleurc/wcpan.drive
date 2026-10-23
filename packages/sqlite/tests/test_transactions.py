import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import quote

from wcpan.drive.sqlite import (
    connect,
    has_pending_wal,
    initialize,
    read_only,
    read_only_dsn,
    read_write,
    uses_wal,
)


class TransactionTestCase(TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.dsn = f"{directory}/test.sqlite"
        initialize(self.dsn)
        with read_write(self.dsn) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO items VALUES (1, 'before')")

    def test_settings(self) -> None:
        with connect(self.dsn) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            self.assertFalse(con.in_transaction)
        with self.assertRaises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")

    def test_read_write_read_and_commit(self) -> None:
        with read_write(self.dsn) as con:
            self.assertEqual(
                con.execute("SELECT value FROM items").fetchone()[0], "before"
            )
            con.execute("UPDATE items SET value = 'after'")
            self.assertEqual(
                con.execute("SELECT value FROM items").fetchone()[0], "after"
            )
            with read_only(self.dsn) as other:
                self.assertEqual(
                    other.execute("SELECT value FROM items").fetchone()[0], "before"
                )
        with read_only(self.dsn) as con:
            self.assertEqual(
                con.execute("SELECT value FROM items").fetchone()[0], "after"
            )

    def test_snapshot_and_read_only_enforcement(self) -> None:
        with read_only(self.dsn) as reader:
            self.assertEqual(
                reader.execute("SELECT value FROM items").fetchone()[0], "before"
            )
            with read_write(self.dsn) as writer:
                writer.execute("UPDATE items SET value = 'after'")
            self.assertEqual(
                reader.execute("SELECT value FROM items").fetchone()[0], "before"
            )
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM items")

    def test_writer_slot_is_reserved_before_first_statement(self) -> None:
        with read_write(self.dsn):
            with self.assertRaises(sqlite3.OperationalError):
                with read_write(self.dsn, timeout=0):
                    self.fail("second writer acquired the slot")

    def test_scripts_do_not_commit_and_ddl_rolls_back(self) -> None:
        for exception in (RuntimeError, KeyboardInterrupt):
            with self.subTest(exception=exception):
                with self.assertRaises(exception):
                    with read_write(self.dsn) as con:
                        con.execute("UPDATE items SET value = 'after'")
                        con.executescript(
                            "CREATE TABLE extra (id INTEGER); INSERT INTO extra VALUES (1);"
                        )
                        self.assertTrue(con.in_transaction)
                        raise exception()
                with read_only(self.dsn) as con:
                    self.assertEqual(
                        con.execute("SELECT value FROM items").fetchone()[0], "before"
                    )
                    self.assertIsNone(
                        con.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'extra'"
                        ).fetchone()
                    )


def _crash_after_insert(dsn: str) -> None:
    """Commit a row into the WAL, then exit without checkpointing."""
    code = (
        "import os, sqlite3\n"
        f"c = sqlite3.connect({dsn!r}, autocommit=True)\n"
        "c.execute('PRAGMA wal_autocheckpoint = 0')\n"
        "c.execute(\"INSERT INTO items VALUES (2, 'crash')\")\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def _rows(con: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in con.execute(sql)]


def _snapshot(directory: str) -> dict[str, bytes]:
    return {
        name: Path(directory, name).read_bytes()
        for name in sorted(os.listdir(directory))
    }


class ReadOnlyDsnTestCase(TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(TemporaryDirectory())
        self.dsn = f"{self.directory}/odd name?#.sqlite"
        initialize(self.dsn)
        with read_write(self.dsn) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO items VALUES (1, 'before')")

    def test_reads_without_writing_any_file(self) -> None:
        before = _snapshot(self.directory)
        with read_only(read_only_dsn(self.dsn)) as con:
            self.assertEqual(_rows(con, "SELECT value FROM items"), [("before",)])
        self.assertEqual(_snapshot(self.directory), before)

    def test_reads_in_a_read_only_directory(self) -> None:
        os.chmod(self.directory, 0o555)
        self.addCleanup(os.chmod, self.directory, 0o755)
        with read_only(read_only_dsn(self.dsn)) as con:
            self.assertEqual(_rows(con, "SELECT id FROM items"), [(1,)])

    def test_writes_are_rejected(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            with read_write(read_only_dsn(self.dsn)) as con:
                con.execute("INSERT INTO items VALUES (3, 'x')")

    def test_pending_wal_is_detected(self) -> None:
        self.assertFalse(has_pending_wal(self.dsn))
        _crash_after_insert(self.dsn)
        self.assertTrue(has_pending_wal(self.dsn))
        # The immutable open does not see what is only in the WAL.
        with read_only(read_only_dsn(self.dsn)) as con:
            self.assertEqual(_rows(con, "SELECT id FROM items"), [(1,)])
        with connect(self.dsn) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertFalse(has_pending_wal(self.dsn))
        with read_only(read_only_dsn(self.dsn)) as con:
            self.assertEqual(_rows(con, "SELECT id FROM items"), [(1,), (2,)])


class UsesWalTestCase(TestCase):
    def test_reads_the_journal_mode_from_the_header(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        wal = f"{directory}/wal.sqlite"
        initialize(wal)
        with read_write(wal) as con:
            con.execute("CREATE TABLE t (x)")
        rollback = f"{directory}/rollback.sqlite"
        with connect(rollback) as con:
            con.execute("CREATE TABLE t (x)")
        self.assertTrue(uses_wal(wal))
        self.assertFalse(uses_wal(rollback))
        self.assertFalse(uses_wal(f"{directory}/missing.sqlite"))


class SymlinkedDatabaseTestCase(TestCase):
    """The WAL of a symlinked database sits next to the real file."""

    def test_pending_wal_of_the_real_file_is_detected(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        real = Path(directory, "real")
        real.mkdir()
        target = str(real / "actual.sqlite")
        initialize(target)
        with read_write(target) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO items VALUES (1, 'before')")
        link = str(Path(directory, "mirror.sqlite"))
        os.symlink(target, link)
        _crash_after_insert(link)
        self.assertFalse(os.path.exists(f"{link}-wal"))
        self.assertTrue(has_pending_wal(link))
        self.assertIn(quote(target), read_only_dsn(link))
