# -*- coding: utf-8 -*-

import re
from unittest import TestCase

from wcpan.drive.client.crypt._lib import (
    decrypt,
    decrypt_name,
    encrypt,
    encrypt_name,
)


class CryptTestCase(TestCase):
    def testBinaryCrypt(self):
        # given
        binary = bytes(range(255))

        # when
        encoded = encrypt(binary)
        # then
        self.assertNotEqual(encoded, binary)
        self.assertEqual(len(encoded), len(binary))

        # when
        decoded = decrypt(encoded)
        # then
        self.assertEqual(binary, decoded)

    def testNameCrypt(self):
        # given
        text = (
            "1234567890"
            "abcdefghijklmnopqrstuvwxyz"
            "().~@-[]{}:,"
            "レオナルド・ディ・セル・ピエーロ・ダ・ヴィンチ"
        )

        # when
        encoded = encrypt_name(text)
        # then
        self.assertNotEqual(encoded, text)
        matched = re.match(r"^[a-z0-9]+$", encoded)
        self.assertIsNotNone(matched)

        # when
        decoded = decrypt_name(encoded)
        # then
        self.assertEqual(text, decoded)
