"""
凭证识别服务（附件 → 识别任务 → 落库 → 与明细比对）
    职责：
        1. 遍历单据明细下的未删除附件，按缓存/幂等规则触发识别；
        2. 调用 document_agent，把结果写入 document_extractions；
        3. 选取每条明细的"主凭证"，生成金额/日期/票号/抬头/凭证强度等风险点；
        4. 任何单张凭证失败都只降级该张，不影响整单 AI 预审。
    边界：
        - 不拼风险评审 Prompt、不调用风险 Agent；
        - 权限校验在 API 层做；
        - 文件读取统一走 storage_service，不直接拼路径。
"""
import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, joinedload, selectinload

from app.agents.document_agent import DocumentAgent, DocumentAgentError, DocumentExtractionResult
from app.config import settings
from app.models import (
    Attachment,
    DocumentExtraction,
    DocumentExtractionStatus,
    Expense,
    ExpenseItem,
)
from app.models.document import EVIDENCE_ORDER
from app.services.document_preprocess import DocumentPreprocessError, prepare_attachment
from app.services.privacy_service import has_ai_consent
from app.services.cost_service import document_daily_limit_reached
from app.services.document_rules import (
    DocumentFacts,
    build_document_findings,
    load_evidence_policy,
    normalize_identifier,
)
from app.services.storage_service import get_storage



logger = logging.getLogger(__name__)

# raw_response 入库上限：防止超长响应把表撑大
MAX_RAW_RESPONSE_CHARS = 20000



# 凭证识别每日限额异常
class DocumentLimitError(Exception):
    """
    凭证识别达到每日调用上限
        由 API 层转成 429；自动流程直接降级为"跳过凭证识别"。
    """
    def __init__(self, message: str = "凭证识别已达到每日调用上限"):
        """
        :param message: 错误说明
        """
        super().__init__(message)
        self.message = message      # 错误说明




# 一次识别任务的结果（服务层返回给上层）
@dataclass(frozen=True)
class DocumentRunResult:
    """
    一张凭证的识别结果
        :ivar extraction: 落库后的识别记录
        :ivar created: 本次是否新执行（False 表示命中缓存）
    """
    extraction: DocumentExtraction        # 识别记录
    created: bool                         # 是否新建任务



# 加载单据及明细/附件
def _load_expense(db: Session, expense_id: int) -> Expense | None:
    """
    加载报销单及其明细/附件
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: Expense 或 None
    """
    return (
        db.query(Expense)
        .options(
            joinedload(Expense.user),                                            # 提交人（授权校验用）
            selectinload(Expense.items).selectinload(ExpenseItem.attachments),   # 明细+附件
            selectinload(Expense.items).selectinload(ExpenseItem.category),      # 明细+类别
        )
        .filter(Expense.id == expense_id)
        .first()
    )



# 查缓存
def _find_cached(db: Session, attachment_id: int) -> DocumentExtraction | None:
    """
    查同一附件在当前 Prompt/模型下的成功识别结果
        :param db: 数据库会话
        :param attachment_id: 附件ID
        :return: 命中缓存返回记录，否则 None
    """
    return (
        db.query(DocumentExtraction)
        .filter(
            DocumentExtraction.attachment_id == attachment_id,
            DocumentExtraction.status == DocumentExtractionStatus.SUCCESS,
            DocumentExtraction.prompt_version == settings.DOCUMENT_PROMPT_VERSION,
            DocumentExtraction.model == settings.DEEPSEEK_MODEL,
        )
        .order_by(DocumentExtraction.id.desc())
        .first()
    )



# 明细上下文（给 Agent 分类用）
def _item_payload(item: ExpenseItem) -> dict:
    """
    组装单条明细的识别上下文
        :param item: 明细实体
        :return: 字典
    """
    category = item.category                              # 费用类别
    return {
        "item_id": item.id,                               # 明细ID
        "category": category.name if category else "",    # 类别名称
        "category_type": (
            category.expense_type.value
            if category is not None and hasattr(category.expense_type, "value")
            else (category.expense_type if category else "")
        ),                                                # 费用大类
        "description": item.description or "",            # 费用说明
        "amount": item.amount,                            # 明细金额
        "expense_date": item.expense_date,                # 费用日期
        "invoice_no": item.invoice_no or "",              # 用户填写的发票号
        "no_invoice_reason": item.no_invoice_reason or "",# 无票说明
    }



# 置信度转 Decimal
def _confidence_decimal(value: float) -> Decimal:
    """
    把 0~1 的置信度转成三位小数 Decimal
        :param value: 浮点置信度
        :return: Decimal
    """
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        number = Decimal("0")
    return max(Decimal("0"), min(number, Decimal("1"))).quantize(Decimal("0.001"))



# 保存识别结果
def _save_result(
    db: Session,
    record: DocumentExtraction,
    result: DocumentExtractionResult,
) -> None:
    """
    把成功识别结果写入记录
        :param db: 数据库会话
        :param record: 识别记录
        :param result: Agent 结果
    """
    record.status = DocumentExtractionStatus.SUCCESS
    record.document_type = result.document_type
    record.evidence_level = result.evidence_level
    record.amount = result.amount
    record.document_date = result.document_date
    record.counterparty = result.counterparty or None
    record.identifier = result.identifier or None
    record.extracted_fields = result.extra or {}
    record.confidence = _confidence_decimal(result.confidence)
    record.needs_manual_review = result.needs_manual_review
    record.model = result.model
    record.prompt_version = result.prompt_version
    record.latency_ms = result.latency_ms
    record.prompt_tokens = result.prompt_tokens
    record.completion_tokens = result.completion_tokens
    record.raw_response = (
        (result.raw_content or "")[:MAX_RAW_RESPONSE_CHARS]
        if settings.DOCUMENT_RAW_RESPONSE_ENABLED
        else None
    )
    record.error_message = None
    db.commit()
    db.refresh(record)



# 保存失败/降级
def _save_failure(db: Session, record: DocumentExtraction, message: str) -> None:
    """
    把任务标记为降级并记录原因
        :param db: 数据库会话
        :param record: 识别记录
        :param message: 失败原因
    """
    record.status = DocumentExtractionStatus.FALLBACK
    record.needs_manual_review = True
    record.error_message = (message or "未知错误")[:500]
    db.commit()
    db.refresh(record)



# 识别单个附件
def extract_attachment(
    db: Session,
    attachment: Attachment,
    item: ExpenseItem,
    *,
    force: bool = False,
    trigger_source: str = "review",
) -> DocumentRunResult:
    """
    识别一张附件（命中缓存则直接复用）
        :param db: 数据库会话
        :param attachment: 附件
        :param item: 所属明细
        :param force: 是否忽略缓存
        :param trigger_source: 触发来源（review/manual）
        :return: DocumentRunResult
    """
    # 1.缓存命中：直接返回历史成功结果
    if not force:
        cached = _find_cached(db, attachment.id)
        if cached is not None:
            return DocumentRunResult(extraction=cached, created=False)

    # 2.每日限额：缓存未命中且已达上限时拒绝新的模型调用
    if document_daily_limit_reached(db):
        raise DocumentLimitError()

    # 3.创建任务记录（先落库，保证失败也有审计）
    record = DocumentExtraction(
        attachment_id=attachment.id,
        expense_item_id=item.id,
        status=DocumentExtractionStatus.PENDING,
        trigger_source=trigger_source,
        provider="deepseek",
        model=settings.DEEPSEEK_MODEL,
        prompt_version=settings.DOCUMENT_PROMPT_VERSION,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # 3.读取文件并预处理
    try:
        with get_storage().open(attachment.file_path) as file_obj:   # 统一存储读取
            content = file_obj.read()                                # 文件字节
        images = prepare_attachment(content, attachment.content_type)   # 图片/PDF → 图片
    except (OSError, DocumentPreprocessError) as exc:
        logger.warning("凭证预处理失败 attachment_id=%s error=%s", attachment.id, exc)
        _save_failure(db, record, getattr(exc, "message", str(exc)))
        return DocumentRunResult(extraction=record, created=True)

    # 4.调用 Agent（同步入口里用 asyncio.run）
    try:
        agent = DocumentAgent()
        result = asyncio.run(agent.extract(images, _item_payload(item)))
    except DocumentAgentError as exc:
        logger.warning("凭证识别失败 attachment_id=%s error=%s", attachment.id, exc.message)
        _save_failure(db, record, exc.message)
        return DocumentRunResult(extraction=record, created=True)
    except Exception as exc:
        logger.exception("凭证识别未预期异常 attachment_id=%s", attachment.id)
        _save_failure(db, record, f"未预期异常: {exc}")
        return DocumentRunResult(extraction=record, created=True)

    # 5.成功落库
    _save_result(db, record, result)
    logger.info(
        "凭证识别完成 attachment_id=%s type=%s amount=%s confidence=%s manual=%s",
        attachment.id,
        record.document_type,
        record.amount,
        record.confidence,
        record.needs_manual_review,
    )
    return DocumentRunResult(extraction=record, created=True)



# 识别一张单据下全部凭证
def extract_for_expense(
    db: Session,
    expense_id: int,
    *,
    force: bool = False,
    trigger_source: str = "review",
) -> list[DocumentExtraction]:
    """
    识别一张报销单下的全部未删除附件
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param force: 是否忽略缓存
        :param trigger_source: 触发来源
        :return: 识别记录列表（含缓存命中）
    """
    records, _usage = _extract_for_expense(
        db,
        expense_id,
        force=force,
        trigger_source=trigger_source,
    )
    return records



# 识别整单并返回本次新增调用的 token 用量
def extract_for_expense_with_usage(
    db: Session,
    expense_id: int,
    *,
    force: bool = False,
    trigger_source: str = "review",
) -> tuple[list[DocumentExtraction], dict]:
    """
    识别整单，并返回本次新建识别任务的 token 用量（缓存命中不计）
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param force: 是否忽略缓存
        :param trigger_source: 触发来源
        :return: (识别记录列表, {"prompt_tokens": int, "completion_tokens": int})
    """
    return _extract_for_expense(
        db,
        expense_id,
        force=force,
        trigger_source=trigger_source,
    )



# 内部实现：整单识别 + 用量统计
def _extract_for_expense(
    db: Session,
    expense_id: int,
    *,
    force: bool = False,
    trigger_source: str = "review",
) -> tuple[list[DocumentExtraction], dict]:
    """
    识别整单并统计本次新增调用的 token
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param force: 是否忽略缓存
        :param trigger_source: 触发来源
        :return: (识别记录列表, 用量字典)
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}   # 本次新增用量

    # 1.总开关关闭：不读取文件、不调用模型
    if not settings.DOCUMENT_AI_ENABLED:
        return [], usage

    expense = _load_expense(db, expense_id)               # 单据
    if expense is None:
        return [], usage

    # 2.隐私闸门：未完成当前版本授权的用户，不读取影像、不调用模型
    if not has_ai_consent(expense.user):
        logger.info("未授权 AI 影像处理，跳过凭证识别 expense_id=%s", expense_id)
        return [], usage

    # 3.每日限额：达到上限时跳过整单凭证识别，不阻断预审
    if document_daily_limit_reached(db):
        logger.info("凭证识别达到每日上限，跳过 expense_id=%s", expense_id)
        return [], usage

    # 4.逐明细、逐附件处理，单张失败不影响其他附件
    results: list[DocumentExtraction] = []                # 结果
    limit = max(1, int(settings.DOCUMENT_MAX_ATTACHMENTS))   # 单次上限
    for item in sorted(expense.items, key=lambda row: row.id):
        for attachment in sorted(item.attachments, key=lambda row: row.id):
            if attachment.is_deleted:
                continue                                  # 软删附件不识别
            if len(results) >= limit:
                logger.info("凭证识别达到单次上限 expense_id=%s limit=%s", expense_id, limit)
                return results, usage
            try:
                run = extract_attachment(
                    db,
                    attachment,
                    item,
                    force=force,
                    trigger_source=trigger_source,
                )
                results.append(run.extraction)
                if run.created:                           # 缓存命中不重复计费
                    usage["prompt_tokens"] += int(run.extraction.prompt_tokens or 0)
                    usage["completion_tokens"] += int(run.extraction.completion_tokens or 0)
            except Exception:
                # 单张异常不能拖垮整单预审
                logger.exception("凭证识别异常已跳过 attachment_id=%s", attachment.id)
    return results, usage



# 检查凭证号是否重复
def _has_duplicate_identifier(db: Session, record: DocumentExtraction, expense_id: int) -> bool:
    """
    检查同一凭证号是否在其他报销单/明细中出现
        :param db: 数据库会话
        :param record: 当前识别记录
        :param expense_id: 当前报销单ID
        :return: 重复返回 True
    """
    identifier = normalize_identifier(record.identifier or "")
    if not identifier:
        return False

    # 1.历史识别记录中的同号凭证（排除当前记录与当前单据）
    duplicate = (
        db.query(DocumentExtraction.id)
        .join(ExpenseItem, DocumentExtraction.expense_item_id == ExpenseItem.id)
        .filter(
            DocumentExtraction.status == DocumentExtractionStatus.SUCCESS,
            DocumentExtraction.identifier == record.identifier,
            DocumentExtraction.id != record.id,
            ExpenseItem.expense_id != expense_id,
        )
        .first()
    )
    if duplicate is not None:
        return True

    # 2.历史明细手工填写的发票号（正式发票才查）
    if record.document_type == "vat_invoice":
        manual = (
            db.query(ExpenseItem.id)
            .filter(
                ExpenseItem.invoice_no == record.identifier,
                ExpenseItem.expense_id != expense_id,
            )
            .first()
        )
        if manual is not None:
            return True
    return False



# 选择主凭证
def _pick_primary(records: list[DocumentExtraction]) -> DocumentExtraction | None:
    """
    从同一明细的多张凭证中选出证明力最高的一张
        排序：证据强度 > 置信度 > 金额
        :param records: 同一明细的识别记录
        :return: 主凭证；没有成功记录返回 None
    """
    successful = [record for record in records if record.status == DocumentExtractionStatus.SUCCESS]
    if not successful:
        return None
    return max(
        successful,
        key=lambda record: (
            EVIDENCE_ORDER.get(record.evidence_level or "none", 0),   # 证据强度
            float(record.confidence or 0),                            # 置信度
            float(record.amount or 0),                                # 金额
        ),
    )



# 生成整单凭证风险点
def collect_document_findings(
    db: Session,
    expense_id: int,
    records: list[DocumentExtraction],
) -> list[dict]:
    """
    把识别结果与明细比对，生成凭证风险点
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param records: 识别记录
        :return: 风险点列表
    """
    if not records:
        return []

    expense = _load_expense(db, expense_id)               # 单据
    if expense is None:
        return []

    policy = load_evidence_policy(db)                    # 阈值策略
    identities = settings.COMPANY_IDENTITIES or []       # 公司抬头
    findings: list[dict] = []                            # 风险点

    # 1.按明细分组，选主凭证
    grouped: dict[int, list[DocumentExtraction]] = {}    # 明细ID → 记录
    for record in records:
        grouped.setdefault(record.expense_item_id, []).append(record)

    for item in expense.items:
        item_records = grouped.get(item.id, [])          # 当前明细的凭证
        primary = _pick_primary(item_records)            # 主凭证
        if primary is None:
            # 全部失败：给一条低风险提示，避免"静默通过"
            if item_records:
                findings.append({
                    "type": "invoice",
                    "level": "low",
                    "message": "凭证识别失败，需人工核对票面信息",
                    "evidence": f"明细「{item.description}」共 {len(item_records)} 张凭证未识别成功",
                    "suggestion": "请上传更清晰的图片，或人工录入发票号/金额",
                    "clause_id": None,
                })
            continue

        facts = DocumentFacts(
            document_type=primary.document_type or "other",
            evidence_level=primary.evidence_level or "weak",
            amount=primary.amount,
            document_date=primary.document_date,
            counterparty=primary.counterparty or "",
            identifier=primary.identifier or "",
            extra=primary.extracted_fields or {},
            confidence=float(primary.confidence or 0),
            needs_manual_review=bool(primary.needs_manual_review),
        )                                                # 比对事实
        duplicate = _has_duplicate_identifier(db, primary, expense_id)   # 凭证号重复
        findings.extend(build_document_findings(
            facts,
            _item_payload(item),
            policy=policy,
            company_identities=identities,
            duplicate_identifier=duplicate,
        ))

    logger.info(
        "凭证风险生成 expense_id=%s records=%s findings=%s",
        expense_id,
        len(records),
        len(findings),
    )
    return findings



# 查询某附件最新识别结果
def get_latest_extraction(db: Session, attachment_id: int) -> DocumentExtraction | None:
    """
    查某附件最新一次识别记录
        :param db: 数据库会话
        :param attachment_id: 附件ID
        :return: 最新记录或 None
    """
    return (
        db.query(DocumentExtraction)
        .filter(DocumentExtraction.attachment_id == attachment_id)
        .order_by(DocumentExtraction.id.desc())
        .first()
    )



# 加载附件所属明细与单据
def load_attachment_context(db: Session, attachment_id: int) -> tuple[Attachment, ExpenseItem, Expense] | None:
    """
    按附件ID加载附件、明细、单据
        :param db: 数据库会话
        :param attachment_id: 附件ID
        :return: (附件, 明细, 单据)；不存在返回 None
    """
    row = (
        db.query(Attachment, ExpenseItem, Expense)
        .join(ExpenseItem, Attachment.expense_item_id == ExpenseItem.id)
        .join(Expense, ExpenseItem.expense_id == Expense.id)
        .filter(Attachment.id == attachment_id)
        .first()
    )
    return row
