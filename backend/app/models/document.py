"""
凭证识别数据模型（document_extractions 表 + 文档类型/证据强度常量）
    背景：
        报销凭证不只有增值税发票，还包括行程单、火车票、打车行程、支付截图、收据等。
        本表记录"一张附件被识别成了什么、提取到哪些字段、证据强度如何、是否成功"，
        供 AI 预审、审批页展示与审计复盘使用。
    设计取舍：
        - 公共字段（金额/日期/对方/凭证号）单独建列，便于查询与比对；
        - 类型专属字段（税号、税额、车次、起终点等）放 extracted_fields JSON，避免频繁改表；
        - 每次识别一行，不覆盖历史；模型/Prompt 升级后可对比旧结果；
        - 本表只追加与状态更新，附件删除时级联清理。
"""
import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.models.base import Base



# 文档类型白名单
DOCUMENT_TYPES = (
    "vat_invoice",          # 增值税发票/电子发票
    "travel_itinerary",     # 机票/差旅行程单
    "train_ticket",         # 火车票/高铁票
    "taxi_receipt",         # 出租车发票/网约车行程单
    "transit_record",       # 公交/地铁乘车记录
    "hotel_folio",          # 酒店水单/账单
    "payment_screenshot",   # 支付/转账截图
    "receipt",              # 收据/小票
    "other",                # 其他可识别凭证
    "unreadable",           # 无法识别
)

# 文档类型 → 证据强度
DOCUMENT_TYPE_EVIDENCE = {
    "vat_invoice": "strong",          # 正式发票：可核对抬头/税号/发票号
    "travel_itinerary": "strong",     # 官方行程单：可核对行程与金额
    "train_ticket": "strong",         # 火车票：可核对车次/座位/金额
    "taxi_receipt": "medium",         # 出租车发票/网约车行程单
    "transit_record": "medium",       # 公交/地铁记录
    "hotel_folio": "medium",          # 酒店水单/账单
    "payment_screenshot": "weak",     # 支付截图：只能核对金额/日期/收款方
    "receipt": "weak",                # 收据/小票：信息有限
    "other": "weak",                  # 其他：默认弱证据
    "unreadable": "none",             # 无法识别：不产生强结论
}

# 证据强度排序：数值越大越强
EVIDENCE_ORDER = {"none": 0, "weak": 1, "medium": 2, "strong": 3}



# 凭证识别任务状态
class DocumentExtractionStatus(str, enum.Enum):
    """
    凭证识别任务状态
        - PENDING：任务已创建，等待执行；
        - RUNNING：正在调用模型；
        - SUCCESS：识别成功且通过结构校验；
        - FALLBACK：识别失败/超时/输出非法，降级为人工核对；
        - FAILED：连降级结果都无法落库（如数据库异常）。
    """
    PENDING = "pending"       # 待执行
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FALLBACK = "fallback"     # 已降级
    FAILED = "failed"         # 失败



# 凭证识别记录表
class DocumentExtraction(Base):
    """
    一张附件的识别记录
        字段分组：
            - 归属：attachment_id、expense_item_id；
            - 执行：status、trigger_source、provider、model、prompt_version；
            - 结果：document_type、evidence_level、amount、document_date、counterparty、identifier；
            - 扩展：extracted_fields（JSON，类型专属字段）；
            - 诊断：confidence、needs_manual_review、raw_response、error_message、耗时/token。
    """
    __tablename__ = "document_extractions"       # 表名：凭证识别记录

    # ===== 主键 =====
    id = Column(
        BigInteger,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="凭证识别记录ID",
    )

    # ===== 归属 =====
    attachment_id = Column(
        BigInteger,
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="附件ID（附件删除时级联清理）",
    )
    expense_item_id = Column(
        BigInteger,
        ForeignKey("expense_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属报销明细ID",
    )

    # ===== 执行信息 =====
    status = Column(
        Enum(
            DocumentExtractionStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            name="document_extraction_status",
        ),
        nullable=False,
        default=DocumentExtractionStatus.PENDING,
        index=True,
        comment="任务状态：pending/running/success/fallback/failed",
    )
    trigger_source = Column(
        String(20),
        nullable=False,
        default="review",
        comment="触发来源：review 随 AI 预审 / manual 手动识别",
    )
    provider = Column(
        String(50),
        nullable=False,
        default="deepseek",
        comment="识别模型提供方",
    )
    model = Column(
        String(100),
        nullable=False,
        default="",
        comment="识别模型名",
    )
    prompt_version = Column(
        String(50),
        nullable=False,
        default="",
        comment="识别 Prompt 版本",
    )

    # ===== 识别结果 =====
    document_type = Column(
        String(30),
        nullable=True,
        comment="文档类型：vat_invoice/taxi_receipt/payment_screenshot 等",
    )
    evidence_level = Column(
        String(20),
        nullable=True,
        comment="证据强度：strong/medium/weak/none",
    )
    amount = Column(
        Numeric(12, 2),
        nullable=True,
        comment="票面/支付金额",
    )
    document_date = Column(
        Date,
        nullable=True,
        comment="票面日期/交易日期",
    )
    counterparty = Column(
        String(200),
        nullable=True,
        comment="商户/平台/收款方",
    )
    identifier = Column(
        String(100),
        nullable=True,
        index=True,
        comment="发票号/订单号/流水号",
    )
    extracted_fields = Column(
        JSON,
        nullable=True,
        comment="类型专属字段（税号/税额/车次/起终点等）",
    )
    confidence = Column(
        Numeric(4, 3),
        nullable=True,
        comment="模型自评置信度 0~1",
    )
    needs_manual_review = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否建议人工核对",
    )

    # ===== 诊断与成本 =====
    raw_response = Column(
        Text,
        nullable=True,
        comment="模型原始返回（审计/排障，不对外透出）",
    )
    error_message = Column(
        String(500),
        nullable=True,
        comment="失败/降级原因",
    )
    latency_ms = Column(
        Integer,
        nullable=True,
        comment="调用耗时（毫秒）",
    )
    prompt_tokens = Column(
        Integer,
        nullable=True,
        comment="输入 token",
    )
    completion_tokens = Column(
        Integer,
        nullable=True,
        comment="输出 token",
    )

    # ===== 关联关系 =====
    attachment = relationship("Attachment")     # 所属附件
    expense_item = relationship("ExpenseItem")  # 所属明细

    def __repr__(self) -> str:
        """
        调试打印：只显示定位信息，不打印原始票面内容
            :return: 形如 <DocumentExtraction(id=1,type=vat_invoice,status=success)>
        """
        return f"<DocumentExtraction(id={self.id},type={self.document_type},status={self.status})>"
