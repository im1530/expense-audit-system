"""
生产环境安全配置校验测试（离线，不联网、不连库）
    覆盖 config.Settings 的 fail-fast 护栏：
        1. 生产环境 SECRET_KEY 过短 -> 拒绝启动；
        2. 生产环境附件加密密钥为空 -> 拒绝启动；
        3. 生产环境附件加密密钥格式非法 -> 拒绝启动；
        4. 生产环境两项都合法 -> 正常通过；
        5. 非生产环境允许短密钥（保证本地开发/CI 不被误伤）。
    说明：本文件只构造 Settings 实例，不触发任何数据库连接或模型调用。
"""
import unittest

from cryptography.fernet import Fernet
from pydantic import ValidationError

import tests  # noqa: F401  先设置测试环境变量
from app.config import Settings



class ProductionSecretValidationTest(unittest.TestCase):
    """生产环境密钥护栏"""

    def _build(self, **overrides):
        """
        构造一个 Settings 实例（未指定的字段仍从 .env 兜底）
            :param overrides: 需要覆盖的配置项
            :return: Settings 实例
        """
        return Settings(**overrides)

    def test_production_rejects_short_secret_key(self):
        """生产环境 SECRET_KEY 过短时必须启动失败"""
        with self.assertRaises(ValidationError) as context:
            self._build(
                APP_ENV="production",                                  # 生产环境
                SECRET_KEY="short",                                    # 故意使用短密钥
                ATTACHMENT_ENCRYPTION_KEY=Fernet.generate_key().decode(),   # 加密密钥合法
            )
        self.assertIn("SECRET_KEY", str(context.exception))            # 错误信息要能定位到具体项

    def test_production_rejects_empty_encryption_key(self):
        """生产环境未配置附件加密密钥时必须启动失败"""
        with self.assertRaises(ValidationError) as context:
            self._build(
                APP_ENV="production",                                  # 生产环境
                SECRET_KEY="x" * 64,                                   # 主密钥长度合法
                ATTACHMENT_ENCRYPTION_KEY="",                          # 故意留空
            )
        self.assertIn("ATTACHMENT_ENCRYPTION_KEY", str(context.exception))   # 错误信息定位到加密密钥

    def test_production_rejects_invalid_encryption_key(self):
        """生产环境附件加密密钥格式非法时必须启动失败"""
        with self.assertRaises(ValidationError) as context:
            self._build(
                APP_ENV="production",                                  # 生产环境
                SECRET_KEY="x" * 64,                                   # 主密钥长度合法
                ATTACHMENT_ENCRYPTION_KEY="not-a-fernet-key",          # 不是合法 Fernet 密钥
            )
        self.assertIn("Fernet", str(context.exception))                # 错误信息说明格式问题

    def test_production_accepts_valid_keys(self):
        """生产环境两项密钥都合法时应正常构造"""
        encryption_key = Fernet.generate_key().decode()                # 合法 Fernet 密钥
        instance = self._build(
            APP_ENV="production",                                      # 生产环境
            SECRET_KEY="s" * 48,                                       # 足够长的主密钥
            ATTACHMENT_ENCRYPTION_KEY=encryption_key,                  # 合法加密密钥
        )
        self.assertEqual(instance.APP_ENV, "production")               # 环境值保留
        self.assertEqual(
            instance.ATTACHMENT_ENCRYPTION_KEY.get_secret_value(),     # 读取 SecretStr 明文
            encryption_key,
        )

    def test_development_allows_short_secret_key(self):
        """非生产环境不应被密钥长度拦住（本地开发便利）"""
        instance = self._build(
            APP_ENV="development",                                     # 开发环境
            SECRET_KEY="short",                                        # 短密钥
            ATTACHMENT_ENCRYPTION_KEY="",                              # 未配置加密
        )
        self.assertEqual(instance.SECRET_KEY, "short")                 # 原样保留

    def test_production_reports_all_problems_at_once(self):
        """两项都不合法时应一次性报出全部缺口，避免反复试错"""
        with self.assertRaises(ValidationError) as context:
            self._build(
                APP_ENV="production",                                  # 生产环境
                SECRET_KEY="",                                         # 主密钥为空
                ATTACHMENT_ENCRYPTION_KEY="",                          # 加密密钥为空
            )
        message = str(context.exception)                               # 错误文本
        self.assertIn("SECRET_KEY", message)                           # 报出主密钥问题
        self.assertIn("ATTACHMENT_ENCRYPTION_KEY", message)            # 同时报出加密密钥问题



if __name__ == "__main__":
    unittest.main()
