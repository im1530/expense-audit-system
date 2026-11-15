"""
自签证书生成脚本测试（离线，不联网、不写仓库目录）
    覆盖 deploy/gen_selfsigned_cert.py：
        1. 能生成 cert.pem / key.pem 两个文件；
        2. 证书 SAN 同时包含 DNS 名称与 IP（否则浏览器按名字访问会报名称不匹配）；
        3. 私钥与证书公钥匹配，且为 2048 位 RSA；
        4. 主题 CN 默认取第一个 DNS 名称；
        5. 没有提供任何名称时明确报错，而不是生成一张不可用的证书。
    说明：脚本位于仓库 deploy/ 目录（不在 app 包内），这里用 importlib 按路径加载，
          避免为了测试把运维脚本塞进应用包。
"""
import datetime
import importlib.util
import pathlib
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import tests  # noqa: F401  先设置测试环境变量



# 仓库根目录（tests/ 的上两级）
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 待测脚本路径
SCRIPT_PATH = REPO_ROOT / "deploy" / "gen_selfsigned_cert.py"



def _load_script_module():
    """
    按文件路径加载 deploy/gen_selfsigned_cert.py
        :return: 模块对象
    """
    spec = importlib.util.spec_from_file_location("gen_selfsigned_cert", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



class SelfSignedCertTest(unittest.TestCase):
    """自签证书生成"""

    @classmethod
    def setUpClass(cls):
        """加载脚本模块（整个类共用）"""
        cls.module = _load_script_module()

    def test_generates_cert_and_key(self):
        """应生成证书与私钥两个文件"""
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = self.module.generate_self_signed(
                pathlib.Path(tmp),
                dns_names=["localhost"],
                ip_addresses=["127.0.0.1"],
            )
            self.assertTrue(cert_path.is_file())                # 证书存在
            self.assertTrue(key_path.is_file())                 # 私钥存在
            self.assertEqual(cert_path.name, "cert.pem")        # 文件名固定，便于 Nginx 引用
            self.assertEqual(key_path.name, "key.pem")

    def test_san_contains_dns_and_ip(self):
        """SAN 必须同时覆盖 DNS 名称与 IP"""
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _key_path = self.module.generate_self_signed(
                pathlib.Path(tmp),
                dns_names=["localhost", "expense.local"],
                ip_addresses=["192.168.1.20"],
            )
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())   # 读回证书
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

            dns_names = set(san.get_values_for_type(x509.DNSName))                  # 取 DNS 条目
            ip_names = {str(item) for item in san.get_values_for_type(x509.IPAddress)}   # 取 IP 条目

            self.assertIn("localhost", dns_names)               # 回环名称
            self.assertIn("expense.local", dns_names)           # 自定义内网名
            self.assertIn("192.168.1.20", ip_names)             # 局域网 IP

    def test_key_matches_certificate(self):
        """私钥必须是 2048 位 RSA 且与证书公钥匹配"""
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = self.module.generate_self_signed(
                pathlib.Path(tmp),
                dns_names=["localhost"],
                ip_addresses=["127.0.0.1"],
            )
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())    # 证书
            private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)   # 私钥

            self.assertIsInstance(private_key, rsa.RSAPrivateKey)                    # 类型正确
            self.assertEqual(private_key.key_size, 2048)                             # 密钥长度
            # 用同一段明文做签名再验签，可证明私钥与证书公钥是一对
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            signature = private_key.sign(b"expense-audit", padding.PKCS1v15(), hashes.SHA256())
            certificate.public_key().verify(signature, b"expense-audit", padding.PKCS1v15(), hashes.SHA256())

    def test_common_name_defaults_to_first_dns(self):
        """主题 CN 默认取第一个 DNS 名称"""
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _key_path = self.module.generate_self_signed(
                pathlib.Path(tmp),
                dns_names=["expense.local", "other.local"],
                ip_addresses=[],
            )
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
            common_name = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            self.assertEqual(common_name, "expense.local")

    def test_validity_period(self):
        """有效期应与传入年数一致（允许 2 天误差）"""
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _key_path = self.module.generate_self_signed(
                pathlib.Path(tmp),
                dns_names=["localhost"],
                ip_addresses=["127.0.0.1"],
                years=1,
            )
            certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
            # 用 *_utc 属性，避免 cryptography 对"naïve datetime"的弃用告警
            delta = certificate.not_valid_after_utc - certificate.not_valid_before_utc
            self.assertGreaterEqual(delta.days, 365 - 2)        # 约一年
            self.assertLessEqual(delta.days, 365 + 2)

    def test_requires_at_least_one_name(self):
        """既没有 DNS 也没有 IP 时必须报错"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.module.generate_self_signed(
                    pathlib.Path(tmp),
                    dns_names=[],
                    ip_addresses=[],
                )



if __name__ == "__main__":
    unittest.main()
