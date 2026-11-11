"""
文件存储服务（本地磁盘实现 + 统一入口）
    目的：把"文件到底存在哪、怎么读回来"从业务代码里隔离出来。
        - 现在用本地磁盘（backend/uploads），以后接 OSS/MinIO 时只需再加一个实现类，
          业务层只依赖 get_storage() 返回的接口；
        - 库里只存相对路径（如 invoices/2026/11/xxx.pdf），换域名或换存储都不用改数据；
        - 所有路径都做"越界校验"：解析后的绝对路径必须仍在存储根目录内，
          防止有人用 ../../ 之类的相对路径读到系统文件；
        - 写文件采用"先写临时文件再原子替换"，避免进程中断留下半截文件被当成正常发票。
    说明：本模块只负责磁盘读写，文件类型/大小校验在 attachment_service 里做。
"""
import io
import os
import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - 依赖由 python-jose[cryptography] 提供
    Fernet = None



# backend 目录（app/services/storage_service.py 往上三层）
BACKEND_DIR = Path(__file__).resolve().parents[2]   # backend 目录

# 加密文件前缀：用于识别"这份文件是 Fernet 密文"，兼容历史明文文件
ENCRYPTION_PREFIX = b"FERNET1:"



# 本地磁盘存储实现
class LocalStorage:
    """
    本地磁盘存储：把文件按 invoices/YYYY/MM/<uuid>.<ext> 的目录结构存放
        为什么按年月分目录：单目录文件过多会拖慢文件系统，也便于按时间归档/清理。
    """

    def __init__(self, root: Path, *, encryption_key: str = ""):
        """
        :param root: 存储根目录（绝对路径）
        :param encryption_key: Fernet 密钥；为空表示不加密
        """
        self.root = root                            # 存储根目录
        self.fernet = None                          # 加密器；未配置密钥时为 None
        if encryption_key:
            if Fernet is None:
                raise RuntimeError("未安装 cryptography，无法启用附件加密")
            self.fernet = Fernet(encryption_key.encode("utf-8"))   # 初始化 Fernet

    @property
    def is_encrypted(self) -> bool:
        """
        当前存储是否启用落盘加密
            :return: 启用返回 True
        """
        return self.fernet is not None

    # 编码：明文 → 落盘字节
    def _encode(self, data: bytes) -> bytes:
        """
        把明文编码成落盘字节（启用密钥时加密并加前缀）
            :param data: 明文字节
            :return: 待写入字节
        """
        if self.fernet is None:
            return data
        return ENCRYPTION_PREFIX + self.fernet.encrypt(data)

    # 解码：落盘字节 → 明文
    def _decode(self, raw: bytes) -> bytes:
        """
        把落盘字节还原成明文（兼容历史无前缀明文文件）
            :param raw: 磁盘原始字节
            :return: 明文字节
        """
        if self.fernet is not None and raw.startswith(ENCRYPTION_PREFIX):
            return self.fernet.decrypt(raw[len(ENCRYPTION_PREFIX):])
        return raw

    # 把相对路径解析成绝对路径，并做越界校验
    def _resolve(self, relative_path: str) -> Path:
        """
        把库里的相对路径解析成绝对路径，并确保没有跑出存储根目录
            :param relative_path: 相对路径，如 invoices/2026/11/xxx.pdf
            :return: 绝对路径
            :raises ValueError: 路径试图越出存储根目录
        """
        # 1.根目录绝对化（避免相对路径受进程工作目录影响）
        root = self.root.resolve()                  # 根目录绝对路径
        # 2.拼接后解析成绝对路径（同时消掉 ../ 这类相对跳转）
        candidate = (root / relative_path).resolve()   # 解析后的绝对路径
        # 3.越界校验：必须严格位于根目录下；用 os.sep 防止 "uploads2" 这种前缀误判
        if candidate != root and not str(candidate).startswith(str(root) + os.sep):
            raise ValueError(f"非法文件路径: {relative_path}")
        # 4.返回安全的绝对路径
        return candidate

    # 生成相对路径（按年月分目录 + UUID 文件名）
    def build_relative_path(self, suffix: str) -> str:
        """
        生成一个新的相对存储路径
            :param suffix: 文件扩展名，如 ".pdf"（带点）
            :return: 形如 invoices/2026/11/<uuid>.pdf 的相对路径
        """
        # 1.取当前时间，用于按年月分目录
        now = datetime.now()                        # 当前时间
        # 2.生成 invoices/YYYY/MM/<uuid>.<ext> 的相对路径（uuid 避免同名覆盖）
        return f"invoices/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{suffix}"

    # 保存文件
    def save(self, relative_path: str, data: bytes) -> None:
        """
        把字节写入磁盘（先写临时文件再原子替换）
            :param relative_path: 目标相对路径（由 build_relative_path 生成）
            :param data: 文件内容
            :raises ValueError: 路径非法
        """
        # 1.解析出目标绝对路径（含越界校验）
        target = self._resolve(relative_path)       # 目标绝对路径
        # 2.父目录不存在就逐级创建
        target.parent.mkdir(parents=True, exist_ok=True)
        # 3.先写临时文件，避免读到半个文件
        payload = self._encode(data)                 # 加密/原样编码
        tmp = target.with_suffix(target.suffix + ".tmp")   # 临时文件路径
        with open(tmp, "wb") as fp:
            fp.write(payload)
        # 4.原子替换：要么是完整文件，要么没有
        os.replace(tmp, target)

    # 打开文件用于下载/预处理
    def open(self, relative_path: str):
        """
        以二进制只读方式打开文件（自动解密；兼容历史明文文件）
            :param relative_path: 相对路径
            :return: 二进制文件对象
            :raises ValueError: 路径非法
            :raises FileNotFoundError: 文件不存在
        """
        path = self._resolve(relative_path)         # 绝对路径
        if self.fernet is None:
            return open(path, "rb")

        raw = path.read_bytes()                     # 读取密文/明文
        return io.BytesIO(self._decode(raw))        # 解密后返回内存流

    # 读取完整字节（下载接口用）
    def read_bytes(self, relative_path: str) -> bytes:
        """
        读取文件完整字节（自动解密）
            :param relative_path: 相对路径
            :return: 明文字节
        """
        with self.open(relative_path) as file_obj:
            return file_obj.read()

    # 判断文件是否存在
    def exists(self, relative_path: str) -> bool:
        """
        判断文件是否还在磁盘上
            :param relative_path: 相对路径
            :return: 存在返回 True
        """
        try:
            return self._resolve(relative_path).is_file()
        except ValueError:
            return False

    # 取绝对路径（供 FileResponse 使用）
    def absolute_path(self, relative_path: str) -> Path:
        """
        把相对路径解析成绝对路径（内部会做越界校验）
            :param relative_path: 相对路径
            :return: 绝对路径
            :raises ValueError: 路径非法
        """
        return self._resolve(relative_path)

    # 删除文件（物理删除，仅清理任务使用）
    def delete(self, relative_path: str) -> None:
        """
        物理删除文件（业务层的"删除附件"只做软删，本方法留给后续清理任务）
            :param relative_path: 相对路径
        """
        try:
            self._resolve(relative_path).unlink(missing_ok=True)
        except ValueError:
            return



# 全局唯一存储实例（延迟创建，避免导入期就读配置）
_storage: LocalStorage | None = None               # 全局存储实例



# 获取存储实现
def get_storage() -> LocalStorage:
    """
    获取全局存储实现（当前固定为本地磁盘）
        UPLOAD_DIR 支持相对路径与绝对路径：
            - 相对路径：相对 backend 目录，如 "uploads" → backend/uploads；
            - 绝对路径：直接用（生产环境建议挂独立数据盘，不要和代码目录混在一起）。
        :return: LocalStorage 实例
    """
    # 1.已经创建过就直接复用（全局单例）
    global _storage
    if _storage is None:
        # 2.解析存储根目录：相对路径按 backend 目录展开，绝对路径原样使用
        root = Path(settings.UPLOAD_DIR)            # 存储根目录
        if not root.is_absolute():
            root = BACKEND_DIR / root               # 相对 backend 目录
        # 3.读取可选的落盘加密密钥（SecretStr 只在内存中使用，不写日志）
        encryption_key = (
            settings.ATTACHMENT_ENCRYPTION_KEY.get_secret_value()
            if settings.ATTACHMENT_ENCRYPTION_KEY
            else ""
        )
        # 4.创建实例并缓存
        _storage = LocalStorage(root, encryption_key=encryption_key)   # 缓存存储实例
    # 4.返回存储实例
    return _storage
