"""
附件落盘加密测试（Phase F）
    覆盖：
        - 配置密钥后编码为带前缀的密文，解码还原明文；
        - 历史明文文件在启用密钥后仍可读取（兼容迁移）；
        - 未配置密钥时保持明文。
    约束：只测试纯编码/解码逻辑，不写磁盘，避免沙箱/CI 目录权限干扰。
"""
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

import tests  # noqa: F401  先加载测试环境，避免 app.config 读正式 .env
from app.services.storage_service import ENCRYPTION_PREFIX, LocalStorage



class StorageEncryptionTest(unittest.TestCase):
    """本地存储加解密"""

    def test_encrypt_and_decrypt(self):
        """编码为密文，解码还原明文"""
        key = Fernet.generate_key().decode()
        storage = LocalStorage(Path("."), encryption_key=key)
        self.assertTrue(storage.is_encrypted)

        encoded = storage._encode(b"plain invoice")
        self.assertTrue(encoded.startswith(ENCRYPTION_PREFIX))
        self.assertNotIn(b"plain invoice", encoded)
        self.assertEqual(storage._decode(encoded), b"plain invoice")

    def test_plaintext_file_still_readable_after_enabling_key(self):
        """启用密钥后仍能读历史明文文件"""
        key = Fernet.generate_key().decode()
        storage = LocalStorage(Path("."), encryption_key=key)
        self.assertEqual(storage._decode(b"legacy"), b"legacy")

    def test_no_key_keeps_plaintext(self):
        """未配置密钥：保持明文"""
        storage = LocalStorage(Path("."))
        self.assertFalse(storage.is_encrypted)
        self.assertEqual(storage._encode(b"plain"), b"plain")
        self.assertEqual(storage._decode(b"plain"), b"plain")
