import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from wcpan.drive.sqlite import checkpoint, connect, initialize, read_only, read_write


class TransactionTestCase(TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.dsn = f"{directory}/test.sqlite"
        initialize(self.dsn)
        with read_write(self.dsn) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO items VALUES (1, 'before')")

    def test_settings_and_checkpoint(self) -> None:
        with connect(self.dsn) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            self.assertFalse(con.in_transaction)
        checkpoint(self.dsn)
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
