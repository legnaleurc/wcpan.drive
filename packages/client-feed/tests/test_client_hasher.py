"""Tests for client hasher selection."""

import hashlib
import pickle
from dataclasses import replace
from datetime import UTC, datetime
from unittest import IsolatedAsyncioTestCase

from Crypto.Hash import MD4
from wcpan.drive.client.exceptions import UnsupportedHashAlgorithmError
from wcpan.drive.client.feed.hasher import (
    WrappedHasher,
    create_md4_hasher,
    get_hasher_factory,
)
from wcpan.drive.client.types import Node


def _make_file(hash_algorithm: str) -> Node:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return Node(
        id="f1",
        parent_id="p",
        name="f.bin",
        is_directory=False,
        is_trashed=False,
        created_time=now,
        modified_time=now,
        changed_time=now,
        mime_type="application/octet-stream",
        hash="00" if hash_algorithm else "",
        hash_algorithm=hash_algorithm,
        size=1,
        is_image=False,
        is_video=False,
        width=0,
        height=0,
        ms_duration=0,
        private=None,
    )


class TestCreateHasher(IsolatedAsyncioTestCase):
    async def test_produces_working_hasher(self):
        # when
        hasher = await create_md4_hasher()
        # then
        await hasher.update(b"hello")
        self.assertEqual(
            await hasher.hexdigest(),
            MD4.new(b"hello").hexdigest(),
        )


class TestGetHasherFactory(IsolatedAsyncioTestCase):
    async def test_selects_per_node_algorithm(self):
        expected = {
            "md4": MD4.new(b"hello").hexdigest(),
            "md5": hashlib.md5(b"hello").hexdigest(),
            "sha1": hashlib.sha1(b"hello").hexdigest(),
            "sha256": hashlib.sha256(b"hello").hexdigest(),
        }
        for algorithm, digest in expected.items():
            with self.subTest(algorithm=algorithm):
                # given
                factory = get_hasher_factory(_make_file(algorithm))
                # when
                hasher = await factory()
                await hasher.update(b"hello")
                # then
                self.assertEqual(await hasher.hexdigest(), digest)

    async def test_factories_are_picklable(self):
        for algorithm in ("md4", "md5", "sha1", "sha256"):
            with self.subTest(algorithm=algorithm):
                # given
                factory = get_hasher_factory(_make_file(algorithm))
                # when
                restored = pickle.loads(pickle.dumps(factory))
                hasher = await restored()
                # then
                self.assertIsInstance(hasher, WrappedHasher)

    def test_unknown_algorithm_raises(self):
        # given
        node = replace(_make_file("md4"), hash_algorithm="crc32")
        # then
        with self.assertRaises(UnsupportedHashAlgorithmError) as ctx:
            get_hasher_factory(node)
        self.assertEqual(ctx.exception.algorithm, "crc32")

    def test_pending_file_raises(self):
        # given
        node = _make_file("")
        # then
        with self.assertRaises(UnsupportedHashAlgorithmError):
            get_hasher_factory(node)


class TestWrappedHasher(IsolatedAsyncioTestCase):
    async def test_hexdigest_and_digest(self):
        # given
        inner = MD4.new()
        h = WrappedHasher(inner)
        # when
        await h.update(b"ab")
        await h.update(b"c")
        hex_d = await h.hexdigest()
        inner2 = MD4.new(b"abc")
        # then
        self.assertEqual(hex_d, inner2.hexdigest())
        self.assertEqual(await WrappedHasher(MD4.new(b"abc")).digest(), inner2.digest())

    async def test_copy_independent(self):
        # given
        h = await create_md4_hasher()
        await h.update(b"x")
        # when
        c = await h.copy()
        await h.update(b"y")
        await c.update(b"z")
        # then
        self.assertEqual(await h.hexdigest(), MD4.new(b"xy").hexdigest())
        self.assertEqual(await c.hexdigest(), MD4.new(b"xz").hexdigest())
