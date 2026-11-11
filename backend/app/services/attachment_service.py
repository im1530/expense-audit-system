"""
附件服务（上传校验 / 元数据落库 / 签名下载地址 / 访问权限）
    职责边界：
        - 路由层负责"这个人能不能对这个明细做这件事"（归属 + 单据状态 + 角色权限）；
        - 本模块负责"这个文件本身合不合规、存到哪、元数据怎么落、地址怎么签"。
    校验要点：
        - 大小：按字节计数，超过 settings.MAX_UPLOAD_MB 直接 413，不落盘；
        - 类型：以文件头（magic number）为准，不信任浏览器传来的 Content-Type；
          只允许 jpg / png / pdf，扩展名由嗅探结果决定，不用用户给的名字；
        - 文件名：只保留 basename 用于展示，磁盘路径完全由 UUID 生成，杜绝路径穿越。
    访问控制：
        - 前端 <img> 不能带 Authorization 头，因此返回"短期签名地址"，
          令牌里带 scope=attachment，只能下载这一张附件，不能拿去做业务请求；
        - 直接带 Bearer 头访问下载接口也可以，但同样要过归属/角色校验。
"""
import os
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.core.security import create_attachment_token
from app.models import Attachment, User
from app.services.storage_service import get_storage



# 文件头签名 → MIME 类型（只认这三种，其余一律拒绝）
MAGIC_SIGNATURES = (                            # 文件头魔数表
    (b"\xff\xd8\xff", "image/jpeg"),            # JPEG
    (b"\x89PNG\r\n\x1a\n", "image/png"),        # PNG
    (b"%PDF", "application/pdf"),               # PDF
)



# MIME → 落盘扩展名（由嗅探结果决定，不采用用户文件名里的后缀）
MIME_TO_SUFFIX = {                              # 类型→扩展名
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}



# 嗅探文件真实类型
def sniff_content_type(head: bytes) -> str | None:
    """
    根据文件头字节判断真实 MIME 类型
        :param head: 文件开头的若干字节（至少 8 字节）
        :return: 识别出的 MIME；不在白名单内或无法识别时返回 None
    """
    # 1.逐个比对文件头魔数（只认 JPG/PNG/PDF）
    for signature, mime in MAGIC_SIGNATURES:
        if head.startswith(signature):
            return mime
    # 2.都不匹配：视为不支持的类型
    return None



# 读取上传文件并做大小限制
async def read_upload_limited(file: UploadFile) -> bytes:
    """
    分块读取上传文件，边读边计数，超过上限立即中断（不把超大文件读进内存/写进磁盘）
        :param file: FastAPI 的 UploadFile
        :return: 文件完整字节内容
        :raises HTTPException: 413 超过大小上限；400 空文件
    """
    # 1.计算字节上限
    max_bytes = int(settings.MAX_UPLOAD_MB * 1024 * 1024)   # 字节上限
    buffer = bytearray()                                    # 读取缓冲区

    # 2.每次读 1MB，边读边计数
    while True:
        chunk = await file.read(1024 * 1024)     # 每次读 1MB
        if not chunk:
            break
        buffer.extend(chunk)
        # 3.超过上限立即中断（不把超大文件读进内存/写进磁盘）
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=413,                        # 请求体过大
                detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 上限",   # 错误提示
            )

    # 4.空文件直接拒绝
    if not buffer:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    # 5.返回完整字节内容
    return bytes(buffer)



# 保存附件
def save_attachment(
    db: Session,                                    # 数据库会话
    *,
    expense_item_id: int,                           # 所属明细ID
    content: bytes,                                 # 文件字节内容
    original_name: str | None,                      # 上传时的原始文件名
    declared_type: str | None,                      # 浏览器声明的类型
    uploader: User,                                 # 上传人
) -> Attachment:
    """
    校验并保存一个附件
        校验顺序：大小已在读取阶段拦住 → 文件头类型 → 声明类型一致性 → 落盘 → 落库。
        落盘成功后才写数据库；数据库写入失败时会把刚写的文件删掉，避免产生孤儿文件。
        :param db: 数据库会话
        :param expense_item_id: 所属报销明细ID（归属与状态校验已在路由层完成）
        :param content: 文件字节
        :param original_name: 用户上传时的原始文件名（仅展示用）
        :param declared_type: 浏览器声明的 Content-Type（仅用于交叉校验）
        :param uploader: 上传人
        :return: 新建的附件实体
        :raises HTTPException: 415 类型不支持/前后不一致；500 写盘失败
    """
    # 1.按文件头判断真实类型
    actual_type = sniff_content_type(content[:8])   # 真实文件类型
    if actual_type is None or actual_type not in settings.ALLOWED_UPLOAD_MIME_TYPES:
        raise HTTPException(
            status_code=415,                        # 不支持的媒体类型
            detail="只支持 JPG/PNG/PDF 文件,请确认文件真实格式(修改扩展名无效)",   # 错误提示
        )

    # 2.声明类型与真实类型交叉校验：不一致说明文件可能被改过
    if declared_type and declared_type not in ("application/octet-stream", actual_type):
        raise HTTPException(
            status_code=415,                        # 不支持的媒体类型
            detail=f"文件类型不一致(声明为 {declared_type},实际为 {actual_type}),请重新上传",   # 错误提示
        )

    # 3.生成存储路径并写盘
    storage = get_storage()                         # 存储实现
    suffix = MIME_TO_SUFFIX[actual_type]            # 落盘扩展名
    relative_path = storage.build_relative_path(suffix)   # 相对存储路径
    try:
        storage.save(relative_path, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="文件保存失败,请稍后重试") from exc

    # 4.落库：文件名只保留 basename，防止把路径信息存进来
    display_name = os.path.basename(original_name or "invoice" + suffix)[:255]   # 展示用文件名
    attachment = Attachment(                        # 附件实体
        expense_item_id=expense_item_id,    # 所属明细
        file_name=display_name,             # 原始文件名(展示用)
        file_path=relative_path,            # 相对存储路径
        content_type=actual_type,           # 真实 MIME
        file_size=len(content),             # 字节数
        uploaded_by=uploader.id,            # 上传人
        is_deleted=False,                   # 默认未删除
    )
    try:
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
    except Exception:
        # 落库失败：把刚写的文件删掉，避免磁盘上留下没有记录的孤儿文件
        db.rollback()
        storage.delete(relative_path)
        raise

    return attachment



# 生成短期签名下载地址
def build_attachment_url(attachment: Attachment) -> str:
    """
    为附件生成带短期签名的下载地址
        为什么签名：前端 <img src> 无法携带 Authorization 头；
        令牌有效期默认 30 分钟，过期后需要重新拉列表拿新地址。
        PUBLIC_BASE_URL 为空时返回相对路径，由前端拼当前域名（开发期最省事）。
        :param attachment: 附件实体
        :return: 可直接放进 img/iframe 的地址
    """
    # 1.为这张附件签发短期下载令牌（scope=attachment）
    token = create_attachment_token(attachment.id, settings.ATTACHMENT_URL_EXPIRE_MINUTES)   # 下载令牌
    # 2.拼下载路径（令牌放 query，前端 img/iframe 可直接用）
    path = f"/api/v1/attachment/{attachment.id}?token={token}"   # 下载路径
    # 3.如果配置了对外域名就补上前缀，否则返回相对路径
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")   # 对外域名前缀
    # 4.返回最终地址
    return f"{base}{path}" if base else path



# 判断用户能否读取附件
def can_read_attachment(user: User, expense_owner_id: int) -> bool:
    """
    判断当前用户是否有权查看某个附件
        规则：报销单提交人本人可看；有审批权限（approve）的审批人可看；有全局查看权限（view_all）的可看。
        :param user: 当前登录用户
        :param expense_owner_id: 附件所属报销单的提交人ID
        :return: 有权限返回 True
    """
    # 1.本人提交的附件：直接可看
    if user.id == expense_owner_id:
        return True
    # 2.审批人（approve）或全局查看者（view_all）可看
    return user.has_permission("approve") or user.has_permission("view_all")



# 软删除附件
def soft_delete_attachment(db: Session, attachment: Attachment) -> None:
    """
    软删除附件（只置标记，不删磁盘文件）
        这样"用户误删/替换发票"后仍可通过运维手段找回；物理文件由后续清理任务按软删标记回收。
        :param db: 数据库会话
        :param attachment: 待删除的附件实体
    """
    attachment.is_deleted = True                    # 软删标记
    db.commit()
