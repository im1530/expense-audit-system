"""
附件（发票/凭证）路由（挂载前缀 /api/v1/attachment）
    覆盖凭证从"上传 → 列表 → 预览/下载 → 识别 → 删除"的完整链路：
        - POST   "/item/{item_id}"  上传发票（multipart），只能给本人草稿/驳回单据的明细上传
        - GET    "/item/{item_id}"  列出某条明细下的全部发票（本人 / 审批人 / 全局查看角色）
        - GET    "/{attachment_id}" 下载/预览（支持短期签名 token，也支持 Bearer 头）
        - POST   "/{attachment_id}/extract"     触发凭证识别（Phase C）
        - GET    "/{attachment_id}/extraction"  查询最新识别结果
        - DELETE "/{attachment_id}" 软删除（本人 + 草稿/驳回状态）
    安全设计：
        - 文件类型按文件头判断，扩展名由嗅探结果决定，用户改扩展名无效；
        - 磁盘路径全部 UUID 生成并做越界校验，库里不存绝对路径；
        - 下载默认走鉴权：签名 token（scope=attachment，短期）或登录用户 + 归属/角色校验；
        - 删除只置软删标记，物理文件留给后续清理任务，避免"DB 未提交但文件已删"。
"""



from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from urllib.parse import quote
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user, get_current_user_optional   # 标准登录 / 可选登录
from app.config import settings                         # 功能开关
from app.core.security import decode_attachment_token   # 附件签名令牌校验
from app.models import Attachment, Expense, ExpenseItem, User
from app.models.expense import ExpenseStatus
from app.schemas.attachment import AttachmentResponse
from app.schemas.document import DocumentExtractionResponse
from app.services.attachment_service import (           # 上传校验 / 签名地址 / 权限判断 / 软删
    build_attachment_url,
    can_read_attachment,
    read_upload_limited,
    save_attachment,
    soft_delete_attachment,
)
from app.services.document_service import (             # 凭证识别 / 查询
    DocumentLimitError,
    extract_attachment,
    get_latest_extraction,
    load_attachment_context,
)
from app.services.privacy_service import AI_CONSENT_REQUIRED_MESSAGE, has_ai_consent   # 影像外发授权
from app.services.storage_service import get_storage    # 存储实现

from typing import List
import logging



# 创建路由器对象
router = APIRouter()  # 路由器对象

# 日志器：发票属于财务凭证，上传/删除都要留痕（谁、对哪条明细、多大文件）
logger = logging.getLogger(__name__)  # 日志器



# 查询明细及其所属单据
def _load_item_with_expense(db: Session, item_id: int) -> tuple[ExpenseItem, Expense] | None:
    """
    按明细ID查出明细与所属报销单（一次联查，避免两次往返）
        :param db: 数据库会话
        :param item_id: 明细ID
        :return: (明细, 报销单)；不存在返回 None
    """
    row = db.query(ExpenseItem, Expense).join(  # 明细与单据
        Expense, ExpenseItem.expense_id == Expense.id
    ).filter(ExpenseItem.id == item_id).first()
    return row



# 组装附件出参
def _build_attachment_response(attachment: Attachment, uploader_name: str | None = None) -> dict:
    """
    把附件实体组装成出参（列表、上传共用）
        :param attachment: 附件实体
        :param uploader_name: 上传人姓名，列表接口会批量查好后传入
        :return: 可直接作为 AttachmentResponse 的字典
    """
    return {
        "id": attachment.id,                            # 附件ID
        "expense_item_id": attachment.expense_item_id,  # 所属明细ID
        "file_name": attachment.file_name,              # 原始文件名
        "content_type": attachment.content_type,        # 真实MIME
        "file_size": attachment.file_size,              # 字节数
        "uploaded_by": attachment.uploaded_by,          # 上传人ID
        "uploaded_by_name": uploader_name,              # 上传人姓名
        "url": build_attachment_url(attachment),        # 签名下载地址
        "created_at": attachment.created_at,            # 上传时间
    }



# 上传发票的接口
@router.post(
    "/item/{item_id}",                  # 接口路径；拼上总前缀后完整为 /api/v1/attachment/item/{item_id}
    response_model=AttachmentResponse,  # 返回新上传的附件（含签名地址）
    tags=["附件"],                       # 在 /api/docs 文档里归到"附件"分组显示
    summary="为报销明细上传发票",          # 接口标题
)
async def upload_invoice(
    item_id: int,                                                   # 路径参数，报销明细ID
    file: UploadFile = File(..., description="发票文件(jpg/png/pdf,≤9.9MB)"),  # 发票文件
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    为指定的报销明细上传一张发票（一张明细可以有多张票）
    权限与状态:
        - 只能给"本人提交的、处于草稿或已驳回状态"的单据上传，其余情况 404/409
        - 提交审批后不允许再补票，避免审批过程中凭证发生变化
    文件校验:
        - 大小：读取时按字节计数，超过 9.9MB 直接 413，不落盘
        - 类型：按文件头判断 JPG/PNG/PDF，改扩展名无效；与浏览器声明类型不一致也拒绝
        - 文件名：只用于展示，磁盘文件名是 UUID
        :param item_id: 路径参数，报销明细ID
        :param file: multipart 上传的文件
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 新附件信息（含短期签名下载地址）
        :raises HTTPException: 404 明细不存在或无权；409 单据状态不允许；413/415 文件不合规
    """
    # 1.查明细与单据
    row = _load_item_with_expense(db, item_id)  # 明细与单据
    if row is None:
        raise HTTPException(status_code=404, detail="报销明细不存在")

    item, expense = row  # 明细与单据

    # 2.归属校验：只能给自己的单据上传（与未找到同样返回 404，不泄露是否存在）
    if expense.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="报销明细不存在或无权操作")

    # 3.状态校验：仅草稿/已驳回可上传
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以上传发票")

    # 4.读取文件（带大小限制）
    content = await read_upload_limited(file)  # 文件字节

    # 5.校验并保存（类型、落盘、落库）
    attachment = save_attachment(  # 新附件
        db,                                     # 数据库会话
        expense_item_id=item.id,                # 附件挂在哪条明细上
        content=content,                        # 文件字节内容
        original_name=file.filename,            # 用户上传时的原始文件名（仅展示）
        declared_type=file.content_type,        # 浏览器声明的 MIME（用于交叉校验）
        uploader=current_user,                  # 上传人（鉴权与审计用）
    )

    # 6.返回（上传人就是当前用户）
    logger.info("上传发票 attachment_id=%s item_id=%s size=%s type=%s user_id=%s",  # 日志模板
                attachment.id, item.id, attachment.file_size, attachment.content_type, current_user.id)  # 附件与上传信息
    return _build_attachment_response(attachment, current_user.full_name)



# 查询明细下全部发票的接口
@router.get(
    "/item/{item_id}",                  # 接口路径；拼上总前缀后完整为 /api/v1/attachment/item/{item_id}
    response_model=List[AttachmentResponse],   # 返回附件列表
    tags=["附件"],                       # 在 /api/docs 文档里归到"附件"分组显示
    summary="查询明细下的发票列表",        # 接口标题
)
def list_item_invoices(
    item_id: int,                                                   # 路径参数，报销明细ID
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    查询某条明细下的全部发票（不含已软删的）
    权限:
        - 报销单提交人本人；或拥有 approve（审批人）/ view_all（财务、老板、管理员）权限的角色
        - 其他人的访问返回 403，避免越权查看他人凭证
        :param item_id: 路径参数，报销明细ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 附件列表（按上传时间升序）
        :raises HTTPException: 404 明细不存在；403 无查看权限
    """
    # 1.查明细与单据
    row = _load_item_with_expense(db, item_id)  # 明细与单据
    if row is None:
        raise HTTPException(status_code=404, detail="报销明细不存在")

    item, expense = row  # 明细与单据

    # 2.权限校验
    if not can_read_attachment(current_user, expense.user_id):
        raise HTTPException(status_code=403, detail="无权限查看该明细的发票")

    # 3.查附件（过滤软删）
    attachments = db.query(Attachment).filter(  # 附件列表
        Attachment.expense_item_id == item.id,  # 所属明细
        Attachment.is_deleted == False,  # 未删除
    ).order_by(Attachment.id.asc()).all()

    # 4.批量取上传人姓名，避免逐条查用户
    uploader_ids = {attachment.uploaded_by for attachment in attachments}  # 上传人ID集合
    name_map = {}  # 姓名映射
    if uploader_ids:
        rows = db.query(User.id, User.full_name).filter(User.id.in_(uploader_ids)).all()  # 上传人记录
        name_map = {row[0]: row[1] for row in rows}  # 姓名映射

    # 5.组装返回
    return [
        _build_attachment_response(attachment, name_map.get(attachment.uploaded_by))
        for attachment in attachments
    ]



# 触发凭证识别的接口
@router.post(
    "/{attachment_id}/extract",                 # 接口路径：/api/v1/attachment/{attachment_id}/extract
    response_model=DocumentExtractionResponse,   # 返回识别结果
    tags=["附件"],
    summary="识别发票/凭证",
)
def extract_invoice(
    attachment_id: int,                                             # 路径参数，附件ID
    force: bool = Query(False, description="是否忽略缓存重新识别"),   # 是否强制重跑
    db: Session = Depends(get_db),                                  # 数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人
):
    """
    对单张发票/凭证执行识别（同步返回）
    权限:
        - 报销单提交人本人；有审批权限或全局查看权限的角色；
        - 已支付/已取消单据不允许再识别。
        :param attachment_id: 路径参数，附件ID
        :param force: 是否忽略缓存
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 识别记录
        :raises HTTPException: 404 附件不存在；403 无权限；409 状态不允许或功能关闭
    """
    # 1.加载附件/明细/单据
    row = load_attachment_context(db, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    attachment, item, expense = row

    # 2.权限与状态校验
    if attachment.is_deleted:
        raise HTTPException(status_code=404, detail="附件已删除")
    if not can_read_attachment(current_user, expense.user_id):
        raise HTTPException(status_code=403, detail="无权限识别该凭证")
    if expense.status in (ExpenseStatus.PAID, ExpenseStatus.CANCELLED):
        raise HTTPException(status_code=409, detail="已支付/已取消单据不允许再识别凭证")
    if not has_ai_consent(expense.user):
        raise HTTPException(status_code=403, detail=AI_CONSENT_REQUIRED_MESSAGE)
    if not settings.DOCUMENT_AI_ENABLED:
        raise HTTPException(status_code=409, detail="凭证识别功能已关闭")

    # 3.执行识别（命中缓存则直接返回）
    try:
        run = extract_attachment(
            db,
            attachment,
            item,
            force=force,
            trigger_source="manual",
        )
    except DocumentLimitError as exc:
        raise HTTPException(status_code=429, detail=exc.message) from exc
    logger.info(
        "手动识别凭证 attachment_id=%s item_id=%s status=%s user_id=%s",
        attachment.id,
        item.id,
        run.extraction.status.value,
        current_user.id,
    )
    return run.extraction



# 查询凭证识别结果的接口
@router.get(
    "/{attachment_id}/extraction",              # 接口路径：/api/v1/attachment/{attachment_id}/extraction
    response_model=DocumentExtractionResponse,  # 返回最新识别结果
    tags=["附件"],
    summary="查询最新凭证识别结果",
)
def get_invoice_extraction(
    attachment_id: int,                                             # 路径参数，附件ID
    db: Session = Depends(get_db),                                  # 数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人
):
    """
    查询某张附件最新一次识别结果
        :param attachment_id: 路径参数，附件ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 识别记录
        :raises HTTPException: 404 附件/识别记录不存在；403 无权限
    """
    # 1.加载附件/明细/单据
    row = load_attachment_context(db, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    attachment, _item, expense = row

    # 2.权限校验
    if attachment.is_deleted:
        raise HTTPException(status_code=404, detail="附件已删除")
    if not can_read_attachment(current_user, expense.user_id):
        raise HTTPException(status_code=403, detail="无权限查看该凭证识别结果")

    # 3.取最新记录
    record = get_latest_extraction(db, attachment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="该附件尚无识别记录")
    return record



# 下载/预览发票的接口
@router.get(
    "/{attachment_id}",                 # 接口路径；拼上总前缀后完整为 /api/v1/attachment/{attachment_id}
    tags=["附件"],                       # 在 /api/docs 文档里归到"附件"分组显示
    summary="下载/预览发票",              # 接口标题
)
def download_invoice(
    attachment_id: int,                                                 # 路径参数，附件ID
    token: str | None = Query(None, description="短期签名令牌(前端img无法带Authorization头时使用)"),  # 签名令牌
    db: Session = Depends(get_db),                                      # 注入数据库会话
    current_user: User | None = Depends(get_current_user_optional),     # 可选登录（签名方式下无登录态）
):
    """
    下载或预览发票文件
    两种鉴权方式（满足其一即可）:
        1. 短期签名：地址里的 ?token=... 由附件接口下发，scope=attachment，默认 30 分钟过期；
        2. 登录用户：带 Authorization: Bearer，且满足"本人 / 有审批权限 / 有全局查看权限"。
    返回: 直接返回文件流（inline），图片可在 img 中预览，PDF 可在 iframe 中打开。
        :param attachment_id: 路径参数，附件ID
        :param token: 可选，短期签名令牌
        :param db: 数据库会话
        :param current_user: 可选，当前登录用户
        :return: 文件流响应
        :raises HTTPException: 404 附件不存在/文件丢失；401 未登录且无有效签名；403 无查看权限
    """
    # 1.查附件（软删的不再提供下载）
    attachment = db.query(Attachment).filter(  # 目标附件
        Attachment.id == attachment_id,  # 附件ID
        Attachment.is_deleted == False,  # 未删除
    ).first()
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在")

    # 2.鉴权：签名令牌优先；没有签名则要求登录 + 归属/角色校验
    authorized = False  # 是否已授权
    if token:
        token_attachment_id = decode_attachment_token(token)  # 令牌附件ID
        if token_attachment_id == attachment.id:
            authorized = True  # 签名通过

    if not authorized:
        if current_user is None:
            raise HTTPException(status_code=401, detail="请先登录或使用有效的下载链接")
        row = _load_item_with_expense(db, attachment.expense_item_id)  # 明细与单据
        if row is None:
            raise HTTPException(status_code=404, detail="附件所属明细不存在")
        _item, expense = row  # 明细与单据
        if not can_read_attachment(current_user, expense.user_id):
            raise HTTPException(status_code=403, detail="无权限查看该发票")

    # 3.文件是否还在磁盘上（软删只删记录不删文件，但文件可能被清理任务回收）
    storage = get_storage()  # 存储实例
    if not storage.exists(attachment.file_path):
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")

    # 4.返回文件内容：统一走 storage.read_bytes，自动兼容明文/加密文件
    filename = quote(attachment.file_name)                       # URL 编码，兼容中文文件名
    return Response(
        content=storage.read_bytes(attachment.file_path),         # 明文字节（内部按需解密）
        media_type=attachment.content_type,                       # 真实 MIME
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{filename}",   # 浏览器内预览
        },
    )



# 删除发票的接口
@router.delete(
    "/{attachment_id}",                 # 接口路径；拼上总前缀后完整为 /api/v1/attachment/{attachment_id}
    tags=["附件"],                       # 在 /api/docs 文档里归到"附件"分组显示
    summary="删除发票(软删)",             # 接口标题
)
def delete_invoice(
    attachment_id: int,                                             # 路径参数，附件ID
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    删除一张发票（软删）
    权限与状态:
        - 仅报销单提交人本人可删；
        - 仅草稿/已驳回状态可删（提交后凭证不允许变动）；
        - 软删只置标记不删磁盘文件，误删可人工恢复。
        :param attachment_id: 路径参数，附件ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 删除结果提示
        :raises HTTPException: 404 附件不存在或无权；409 状态不允许
    """
    # 1.查附件
    attachment = db.query(Attachment).filter(  # 目标附件
        Attachment.id == attachment_id,  # 附件ID
        Attachment.is_deleted == False,  # 未删除
    ).first()
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在")

    # 2.查明细与单据，做归属与状态校验
    row = _load_item_with_expense(db, attachment.expense_item_id)  # 明细与单据
    if row is None:
        raise HTTPException(status_code=404, detail="附件所属明细不存在")

    _item, expense = row  # 明细与单据
    if expense.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="附件不存在或无权操作")
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以删除发票")

    # 3.软删除
    soft_delete_attachment(db, attachment)
    logger.info("删除发票(软删) attachment_id=%s item_id=%s user_id=%s",  # 日志模板
                attachment.id, attachment.expense_item_id, current_user.id)  # 附件/明细/操作人
    return {"message": "发票已删除", "id": attachment_id}
