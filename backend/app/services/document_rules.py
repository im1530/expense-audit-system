"""
凭证一致性规则（票面事实 vs 报销明细）
    原则：
        - 金额不足、发票号不一致、抬头不符属于客观冲突，直接给 high；
        - 日期偏差、弱/中等凭证金额超阈值属于"需要人工复核"，给 mid；
        - 识别失败/类型未知只提示人工核对，不给强结论；
        - 金额不一致与凭证强弱无关；"没有发票号/税号"不能对支付截图误报。
    设计：
        - 纯计算，不查库、不写库；
        - 重复凭证号由 service 查库后以布尔值传入；
        - 阈值来自 rules 表的 check_evidence_strength 配置，缺省回落到 settings。
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Rule
from app.models.expense import ExpenseType



# 合法费用大类：用于校验类别阈值的键
ALLOWED_EXPENSE_TYPES = tuple(item.value for item in ExpenseType)

# 空白/连字符：凭证号比对时忽略
IDENTIFIER_NOISE = re.compile(r"[\s\-]")

# 公司名归一化时去掉的标点/空白
COMPANY_NOISE = re.compile(r"[\s（）()【】\[\]·,，.。]")



# 证据强度阈值策略
@dataclass(frozen=True)
class EvidencePolicy:
    """
    弱/中等凭证的免复核金额策略
        :ivar weak_max_amount: 弱凭证默认免复核上限
        :ivar medium_max_amount: 中等凭证默认免复核上限
        :ivar category_overrides: 按费用大类覆盖弱凭证上限，如 {"transportation": 300}
    """
    weak_max_amount: Decimal = Decimal("200.00")                 # 弱凭证默认上限
    medium_max_amount: Decimal = Decimal("1000.00")              # 中等凭证默认上限
    category_overrides: dict[str, Decimal] = field(default_factory=dict)   # 类别覆盖

    def weak_limit(self, expense_type: str) -> Decimal:
        """
        取某个费用大类的弱凭证上限
            :param expense_type: 费用大类
            :return: 阈值
        """
        return self.category_overrides.get(expense_type, self.weak_max_amount)



# 参与比对的最小凭证事实
@dataclass(frozen=True)
class DocumentFacts:
    """
    从识别结果/ORM 中抽出的比对字段
        :ivar document_type: 文档类型
        :ivar evidence_level: 证据强度
        :ivar amount: 金额
        :ivar document_date: 票面日期
        :ivar counterparty: 商户/平台
        :ivar identifier: 凭证号
        :ivar extra: 类型专属字段
        :ivar confidence: 置信度
        :ivar needs_manual_review: 是否建议人工核对
    """
    document_type: str                                          # 文档类型
    evidence_level: str                                         # 证据强度
    amount: Decimal | None = None                               # 金额
    document_date: date | None = None                           # 票面日期
    counterparty: str = ""                                      # 商户/平台
    identifier: str = ""                                        # 凭证号
    extra: dict = field(default_factory=dict)                   # 类型专属字段
    confidence: float = 0.0                                     # 置信度
    needs_manual_review: bool = False                           # 是否人工核对



# 安全转 Decimal
def _to_decimal(value, default: Decimal | None = None) -> Decimal | None:
    """
    把配置/事实值转成 Decimal
        :param value: 原始值
        :param default: 失败时的默认值
        :return: Decimal 或默认值
    """
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default



# 加载证据阈值策略
def load_evidence_policy(db: Session) -> EvidencePolicy:
    """
    从 rules 表读取 check_evidence_strength 配置，缺省回落到 settings
        :param db: 数据库会话
        :return: EvidencePolicy
    """
    # 1.默认值来自配置
    weak_default = _to_decimal(settings.DOCUMENT_WEAK_EVIDENCE_MAX_AMOUNT, Decimal("200.00"))
    medium_default = _to_decimal(settings.DOCUMENT_MEDIUM_EVIDENCE_MAX_AMOUNT, Decimal("1000.00"))

    # 2.查启用中的规则（只取第一条，管理端应保证唯一）
    rule = (
        db.query(Rule)
        .filter(
            Rule.rule_type == "check_evidence_strength",
            Rule.is_active == True,
        )
        .order_by(Rule.id.asc())
        .first()
    )
    if rule is None or not isinstance(rule.config, dict):
        return EvidencePolicy(
            weak_max_amount=weak_default,
            medium_max_amount=medium_default,
        )

    # 3.解析阈值
    config = rule.config
    weak = _to_decimal(config.get("weak_max_amount"), weak_default)
    medium = _to_decimal(config.get("medium_max_amount"), medium_default)
    if weak is None or weak < 0:
        weak = weak_default
    if medium is None or medium < weak:
        medium = max(weak, medium_default)

    # 4.解析类别覆盖（非法键/值直接忽略，避免脏配置拖垮预审）
    overrides: dict[str, Decimal] = {}
    raw_overrides = config.get("category_overrides")
    if isinstance(raw_overrides, dict):
        for key, value in raw_overrides.items():
            expense_type = str(key).strip()
            threshold = _to_decimal(value)
            if expense_type in ALLOWED_EXPENSE_TYPES and threshold is not None and threshold >= 0:
                overrides[expense_type] = threshold

    return EvidencePolicy(
        weak_max_amount=weak,
        medium_max_amount=medium,
        category_overrides=overrides,
    )



# 公司名归一化
def normalize_company_name(value: str) -> str:
    """
    归一化公司名称，降低括号/空白/全半角造成的误报
        :param value: 原始名称
        :return: 归一化名称
    """
    text = unicodedata.normalize("NFKC", str(value or "")).lower()   # 全半角/大小写
    return COMPANY_NOISE.sub("", text).strip()                       # 去标点空白



# 凭证号归一化
def normalize_identifier(value: str) -> str:
    """
    归一化发票号/订单号，忽略空格与连字符
        :param value: 原始凭证号
        :return: 归一化凭证号
    """
    text = unicodedata.normalize("NFKC", str(value or "")).upper()   # 全半角/大写
    return IDENTIFIER_NOISE.sub("", text).strip()                    # 去空格/连字符



# 购买方是否命中允许抬头
def company_identity_match(facts: DocumentFacts, identities: list[dict]) -> bool | None:
    """
    判断发票购买方是否属于公司允许抬头
        :param facts: 凭证事实
        :param identities: 公司身份列表，如 [{"name": "...", "tax_id": "..."}]
        :return: True 命中 / False 不匹配 / None 未配置或票面信息不足
    """
    if not identities:
        return None

    buyer_name = normalize_company_name(facts.extra.get("buyer_name") or "")
    buyer_tax_id = normalize_identifier(facts.extra.get("buyer_tax_id") or "")
    if not buyer_name and not buyer_tax_id:
        return None

    allowed_names = {normalize_company_name(item.get("name") or "") for item in identities}   # 允许名称
    allowed_tax_ids = {normalize_identifier(item.get("tax_id") or "") for item in identities}  # 允许税号
    if buyer_tax_id and buyer_tax_id in allowed_tax_ids:
        return True
    if buyer_name and buyer_name in allowed_names:
        return True
    return False



# 构造一条风险点
def _finding(level: str, message: str, evidence: str, suggestion: str) -> dict:
    """
    构造统一格式的凭证风险点
        :param level: high/mid/low
        :param message: 提示
        :param evidence: 证据
        :param suggestion: 建议
        :return: finding 字典
    """
    return {
        "type": "invoice",          # 沿用现有 invoice 类型，前端按凭证风险展示
        "level": level,             # 风险级别
        "message": message[:500],   # 提示
        "evidence": evidence[:500], # 证据
        "suggestion": suggestion[:300],  # 建议
        "clause_id": None,          # 凭证风险不绑定制度条款
    }



# 生成凭证风险点
def build_document_findings(
    facts: DocumentFacts,
    item: dict,
    *,
    policy: EvidencePolicy,
    company_identities: list[dict] | None = None,
    duplicate_identifier: bool = False,
) -> list[dict]:
    """
    把凭证事实与明细进行一致性比对
        :param facts: 凭证事实
        :param item: 明细上下文（amount/expense_date/invoice_no/category_type/description）
        :param policy: 证据阈值策略
        :param company_identities: 允许的公司抬头
        :param duplicate_identifier: 该凭证号是否已在其他单据出现
        :return: 风险点列表
    """
    findings: list[dict] = []                                   # 风险点
    item_amount = _to_decimal(item.get("amount"), Decimal("0")) or Decimal("0")   # 明细金额
    item_date = item.get("expense_date")                        # 费用日期
    item_invoice_no = normalize_identifier(item.get("invoice_no") or "")   # 明细填写的发票号
    expense_type = str(item.get("category_type") or "")         # 费用大类
    tolerance = _to_decimal(settings.DOCUMENT_AMOUNT_TOLERANCE, Decimal("0.01")) or Decimal("0.01")   # 金额容差

    # 1.无法识别/类型未知：只提示人工核对
    if facts.document_type == "unreadable":
        findings.append(_finding(
            "low",
            "凭证无法识别，需人工核对票面信息",
            f"模型置信度 {facts.confidence:.2f}",
            "请上传更清晰的图片，或人工录入发票号/金额",
        ))
        return findings

    # 2.金额比对：任何凭证类型都适用
    if facts.amount is not None and item_amount > 0:
        difference = item_amount - facts.amount                # 明细金额 - 票面金额
        if difference > tolerance:
            findings.append(_finding(
                "high",
                f"凭证金额 {facts.amount} 元低于明细金额 {item_amount} 元，差额 {difference} 元",
                f"凭证类型 {facts.document_type}，凭证号 {facts.identifier or '未识别'}",
                "核对发票/支付记录，确认是否上传错票或金额填写有误",
            ))
        elif -difference > tolerance:
            findings.append(_finding(
                "mid",
                f"凭证金额 {facts.amount} 元高于明细金额 {item_amount} 元",
                f"差额 {-difference} 元",
                "确认是否为部分报销、同一凭证覆盖多条明细，或金额填写有误",
            ))

    # 3.发票号比对：只对正式发票
    if facts.document_type == "vat_invoice" and item_invoice_no:
        document_identifier = normalize_identifier(facts.identifier)
        if document_identifier and document_identifier != item_invoice_no:
            findings.append(_finding(
                "high",
                "票面发票号与明细填写的发票号不一致",
                f"票面 {facts.identifier}，明细 {item.get('invoice_no')}",
                "以票面为准修正明细，或确认是否上传错票",
            ))

    # 4.购买方抬头：只对正式发票，且配置了公司身份
    if facts.document_type == "vat_invoice" and company_identities:
        match = company_identity_match(facts, company_identities)
        buyer_name = facts.extra.get("buyer_name") or ""
        buyer_tax_id = facts.extra.get("buyer_tax_id") or ""
        if match is False:
            findings.append(_finding(
                "high",
                "发票购买方不是本公司允许抬头",
                f"购买方 {buyer_name or '未识别'}，税号 {buyer_tax_id or '未识别'}",
                "确认发票抬头/税号是否正确，错误发票需退回重开",
            ))
        elif match is None:
            findings.append(_finding(
                "mid",
                "发票未识别到购买方信息，需人工核对抬头",
                "票面未提取到购买方名称/税号",
                "核对发票抬头与税号是否为公司主体",
            ))

    # 5.日期偏差：所有凭证类型
    if facts.document_date is not None and isinstance(item_date, date):
        deviation = abs((facts.document_date - item_date).days)   # 天数偏差
        tolerance_days = max(0, int(settings.DOCUMENT_DATE_TOLERANCE_DAYS))
        if deviation > tolerance_days:
            findings.append(_finding(
                "mid",
                f"票面日期 {facts.document_date} 与费用日期 {item_date} 相差 {deviation} 天",
                f"允许偏差 {tolerance_days} 天",
                "确认费用发生日期是否填写正确，或凭证是否对应本次报销",
            ))

    # 6.重复凭证号：发票号 high，订单号/流水号 mid
    if duplicate_identifier and facts.identifier:
        level = "high" if facts.document_type == "vat_invoice" else "mid"
        findings.append(_finding(
            level,
            "该凭证号已在其他报销单中出现，疑似重复报销",
            f"凭证号 {facts.identifier}",
            "核对历史单据，确认是否为同一笔费用重复提交",
        ))

    # 7.证据强度阈值：弱/中等凭证金额超限时提示复核
    if facts.amount is not None and facts.evidence_level in ("weak", "medium"):
        if facts.evidence_level == "weak":
            limit = policy.weak_limit(expense_type)
            label = "弱凭证"
        else:
            limit = policy.medium_max_amount
            label = "中等凭证"
        if facts.amount > limit:
            findings.append(_finding(
                "mid",
                f"{label}金额 {facts.amount} 元超过免复核阈值 {limit} 元",
                f"凭证类型 {facts.document_type}，证据强度 {facts.evidence_level}",
                "建议补充正式发票/行程单，或由审批人重点核对",
            ))

    # 8.关键字段缺失/低置信度：没有其他结论时给一条 low 提示
    if not findings and facts.needs_manual_review:
        findings.append(_finding(
            "low",
            "凭证关键字段未识别或置信度较低，建议人工核对",
            f"置信度 {facts.confidence:.2f}",
            "核对票面金额、日期与发票号",
        ))

    return findings
