"""
生成内网/无域名环境使用的自签名证书（Nginx 可直接读取的 PEM）。

适用场景：
    - 系统部署在公司内网，没有公网域名、也不打算做 ICP 备案；
    - 又希望浏览器到 Nginx 这一段走 HTTPS（密码与令牌不明文过内网）。

产出：
    <out-dir>/cert.pem  公钥证书（Nginx ssl_certificate）
    <out-dir>/key.pem   私钥（Nginx ssl_certificate_key，应限制文件权限）

依赖：cryptography（后端 requirements.txt 已包含，部署机必然具备），不需要 OpenSSL。

重要提醒：
    1. 自签证书不会被浏览器默认信任，需要把 cert.pem 装进客户端的"受信任的根证书"；
    2. 证书必须覆盖客户端实际访问用的名字——用 IP 访问就要写 IP，用主机名访问就要写主机名，
       否则即使装了根证书，浏览器仍会报"名称不匹配"；
    3. 私钥 key.pem 泄露 = 任何人都能冒充该站点，不要提交到仓库、不要随代码分发。
"""
import argparse
import datetime
import ipaddress
import pathlib
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


# 证书有效期上限说明：自签证书过期后浏览器会直接拦截，建议留出更换时间
DEFAULT_YEARS = 3


def build_subject_alt_names(dns_names: list[str], ip_addresses: list[str]) -> x509.SubjectAlternativeName:
    """
    组装证书的"使用者可选名称"（SAN）
        :param dns_names: 需要覆盖的 DNS 名称，如 localhost、expense.corp.local
        :param ip_addresses: 需要覆盖的 IP 地址，如 127.0.0.1、192.168.1.20
        :return: SAN 扩展对象
        :raises ValueError: 没有提供任何名称，或 IP 格式非法
    """
    # 1.逐条解析：DNS 名称保持原样，IP 必须能被 ipaddress 解析（否则证书无法用于该地址）
    entries: list[x509.GeneralName] = []
    for name in dns_names:
        clean = name.strip()                        # 去掉首尾空白
        if clean:
            entries.append(x509.DNSName(clean))     # DNS 类型条目
    for address in ip_addresses:
        clean = address.strip()                     # 去掉首尾空白
        if clean:
            entries.append(x509.IPAddress(ipaddress.ip_address(clean)))   # IP 类型条目

    # 2.没有任何条目时直接报错，避免生成一张"谁都用不了"的证书
    if not entries:
        raise ValueError("至少需要提供一个 DNS 名称或 IP 地址")
    return x509.SubjectAlternativeName(entries)


def generate_self_signed(
    output_dir: pathlib.Path,
    *,
    dns_names: list[str],
    ip_addresses: list[str],
    common_name: str | None = None,
    years: int = DEFAULT_YEARS,
) -> tuple[pathlib.Path, pathlib.Path]:
    """
    生成一对自签名证书与私钥，并写入输出目录
        :param output_dir: 输出目录（不存在会自动创建）
        :param dns_names: 证书覆盖的 DNS 名称
        :param ip_addresses: 证书覆盖的 IP 地址
        :param common_name: 主题 CN；不传时取第一个 DNS 名称
        :param years: 有效期（年）
        :return: (证书路径, 私钥路径)
    """
    # 1.准备主题：CN 必须有值，否则部分客户端会认为证书信息不完整
    cn_value = common_name or (dns_names[0] if dns_names else (ip_addresses[0] if ip_addresses else ""))
    if not cn_value:
        raise ValueError("无法确定证书主题（请提供 -DnsNames 或 -IpAddresses）")
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn_value)])

    # 2.生成 2048 位 RSA 私钥（Nginx 与所有主流浏览器都支持）
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # 3.计算有效期：起始时间往前留 1 天，避免客户端与服务器时钟略有偏差时被判"尚未生效"
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(days=1)
    not_after = now + datetime.timedelta(days=365 * max(1, years))

    # 4.组装证书：
    #    - 自签（issuer = subject）；
    #    - CA=False：这是服务器证书，不是 CA 证书；
    #    - EKU=serverAuth：明确用途是 TLS 服务端；
    #    - SAN 必须包含客户端访问用的名字/IP。
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(build_subject_alt_names(dns_names, ip_addresses), critical=False)
        .sign(private_key, hashes.SHA256())
    )

    # 5.写文件：证书用 PEM；私钥用 PKCS#8 PEM（Nginx 可直接读），权限尽量收紧
    output_dir.mkdir(parents=True, exist_ok=True)
    cert_path = output_dir / "cert.pem"
    key_path = output_dir / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        key_path.chmod(0o600)                       # 仅当前用户可读（Windows 上效果有限，仍建议）
    except OSError:
        pass
    return cert_path, key_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数
        :param argv: 参数列表（测试可注入）
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="生成内网自签证书（Nginx 用 PEM）")
    parser.add_argument(
        "--dns", dest="dns_names", action="append", default=None,
        help="证书覆盖的 DNS 名称，可重复传入，如 --dns localhost --dns expense.corp.local",
    )
    parser.add_argument(
        "--ip", dest="ip_addresses", action="append", default=None,
        help="证书覆盖的 IP，可重复传入，如 --ip 192.168.1.20",
    )
    parser.add_argument("--out-dir", default="deploy/certs", help="输出目录（默认 deploy/certs，已被 .gitignore 忽略）")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="有效期（年）")
    parser.add_argument("--common-name", default=None, help="主题 CN（默认取第一个 DNS 名称）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    命令行入口
        :param argv: 命令行参数
        :return: 进程退出码（0 成功，1 参数或运行错误）
    """
    args = parse_args(argv)
    dns_names = args.dns_names or ["localhost"]           # 未指定时至少覆盖 localhost
    ip_addresses = args.ip_addresses or ["127.0.0.1"]     # 未指定时至少覆盖本机回环

    try:
        cert_path, key_path = generate_self_signed(
            pathlib.Path(args.out_dir),
            dns_names=dns_names,
            ip_addresses=ip_addresses,
            common_name=args.common_name,
            years=args.years,
        )
    except Exception as exc:                              # noqa: BLE001 - 命令行工具需要给出可读错误
        print(f"[错误] 生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # 输出结果与后续动作（不打印私钥内容）
    print("证书已生成：")
    print(f"  证书: {cert_path}")
    print(f"  私钥: {key_path}")
    print(f"  覆盖: {', '.join([f'dns={item}' for item in dns_names] + [f'ip={item}' for item in ip_addresses])}")
    print("")
    print("Nginx 配置：")
    print(f"  ssl_certificate     {cert_path.as_posix()};")
    print(f"  ssl_certificate_key {key_path.as_posix()};")
    print("")
    print("客户端信任（每台要访问的电脑执行一次，需管理员）：")
    print(f"  Import-Certificate -FilePath '{cert_path}' -CertStoreLocation Cert:\\LocalMachine\\Root")
    print("")
    print("提示：用 IP 访问时证书必须包含那个 IP；换机器/换 IP 后要重新生成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
