"""Tests for client MD4 hasher."""

from unittest import IsolatedAsyncioTestCase

from Crypto.Hash import MD4
from wcpan.drive.client.remote.hasher import Md4Hasher, create_hasher


class TestCreateHasher(IsolatedAsyncioTestCase):
    async def test_produces_working_hasher(self):
        # when
        hasher = await create_hasher()
        # then
        await hasher.update(b"hello")
        self.assertEqual(
            await hasher.hexdigest(),
            MD4.new(b"hello").hexdigest(),
        )


class TestMd4Hasher(IsolatedAsyncioTestCase):
    async def test_hexdigest_and_digest(self):
        # given
        inner = MD4.new()
        h = Md4Hasher(inner)
        # when
        await h.update(b"ab")
        await h.update(b"c")
        hex_d = await h.hexdigest()
        inner2 = MD4.new(b"abc")
        # then
        self.assertEqual(hex_d, inner2.hexdigest())
        self.assertEqual(await Md4Hasher(MD4.new(b"abc")).digest(), inner2.digest())

    async def test_copy_independent(self):
        # given
        h = await create_hasher()
        await h.update(b"x")
        # when
        c = await h.copy()
        await h.update(b"y")
        await c.update(b"z")
        # then
        self.assertEqual(await h.hexdigest(), MD4.new(b"xy").hexdigest())
        self.assertEqual(await c.hexdigest(), MD4.new(b"xz").hexdigest())
