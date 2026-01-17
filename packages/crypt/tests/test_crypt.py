import re
from unittest import TestCase

from wcpan.drive.crypt._lib import (
    decrypt,
    decrypt_name,
    encrypt,
    encrypt_name,
)


class CryptTestCase(TestCase):
    def testBinaryCrypt(self):
        binary = bytes(range(255))

        encoded = encrypt(binary)
        self.assertNotEqual(encoded, binary)
        self.assertEqual(len(encoded), len(binary))

        decoded = decrypt(encoded)
        self.assertEqual(binary, decoded)

    def testNameCrypt(self):
        text = (
            "1234567890"
            "abcdefghijklmnopqrstuvwxyz"
            "().~@-[]{}:,"
            "レオナルド・ディ・セル・ピエーロ・ダ・ヴィンチ"
        )

        encoded = encrypt_name(text)
        self.assertNotEqual(encoded, text)
        matched = re.match(r"^[a-z0-9]+$", encoded)
        self.assertIsNotNone(matched)

        decoded = decrypt_name(encoded)
        self.assertEqual(text, decoded)
