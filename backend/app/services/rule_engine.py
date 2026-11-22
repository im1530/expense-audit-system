"""
规则引擎（检查型规则：只做风险提示，不改审批流）
    与 amount 路由规则的分工：
        - amount（services/expense_service.match_approval_flow）：按角色+金额决定"该谁审"；
        - check_*（本模块）：判断"这单有没有问题"，产出可解释的提示给审批人看。
    当前实现的检查规则（规则参数都在 rules.config 里，可在管理端改）：
        - check_category_limit   单条明细金额 > 类别单次限额（categories.max_amount）
        - check_invoice_missing  明细既无发票文件、无发票号，又没填无票说明
        - check_duplicate        同人 + 同费用日期 + 同类别 + 同金额，在窗口期内出现多次
        - check_period_late      费用发生日距提交日超过 N 天
    设计原则：
        - 纯计算：run_rule_checks 不写库，方便单测、预检、以后给 LLM 复用；
        - 规则隔离：单条规则抛异常只记日志、跳过，不拖垮整次提交；
        - 结果落库：save_rule_hits 先清空旧记录再写入，保证"一单一份最新结果"。
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Expense, ExpenseRuleHit, ExpenseItem, Rule
from app.models.expense import ExpenseStatus



logger = logging.getLogger(__name__)            # 日志器



# 检查型规则类型（与 rule_service 的白名单保持一致）
# 注意：check_evidence_strength 由 AI 预审阶段的 document_service 执行（需要凭证识别结果），
# 不进入提交时的规则引擎，因此不在本元组里。
CHECK_RULE_TYPES = (                            # 检查型规则类型
    "check_category_limit",
    "check_invoice_missing",
    "check_duplicate",
    "check_period_late",
)

# 检查规则 → 制度条款映射：让规则结论和 AI 引用一样可追溯
RULE_CLAUSE_MAP = {
    "check_category_limit": "3.7",       # 超限/限额
    "check_invoice_missing": "2.5",      # 凭证缺失
    "check_duplicate": "4.3",            # 重复报销
    "check_period_late": "4.2",          # 跨期报销
    "check_evidence_strength": "2.2",    # 有效凭证
}

# 级别优先级：数字越大越严重，用于汇总单据整体风险级别
LEVEL_ORDER = {"warn": 1, "review": 2, "block": 3}   # 级别优先级



# 构造一条命中结果
def _build_hit(rule: Rule, level: str, message: str, evidence: dict) -> dict:
    """
    组装单条命中结果（尚未落库）
        :param rule: 命中的规则
        :param level: 级别 warn/review/block
        :param message: 给审批人看的提示
        :param evidence: 命中证据（实际值、阈值、关联单据等）
        :return: 命中字典
    """
    return {
        "rule_id": rule.id,                     # 规则ID
        "rule_type": rule.rule_type,            # 规则类型
        "rule_name": rule.name,                 # 规则名称
        "clause_id": RULE_CLAUSE_MAP.get(rule.rule_type),  # 制度条款ID
        "level": level,                         # 命中级别
        "message": message,                     # 提示文案
        "evidence": evidence,                   # 命中证据
    }



# 取规则配置的级别
def _rule_level(rule: Rule) -> str:
    """
    从规则 severity 字段取提示级别（warn/block/review），非法值按 warn 处理
        :param rule: 规则实体
        :return: 级别字符串
    """
    severity = (rule.severity or "warn").strip().lower()   # 检查规则风险级别
    return severity if severity in LEVEL_ORDER else "warn"



# 规则1：类别单次限额
def _check_category_limit(db: Session, expense: Expense, rule: Rule) -> list[dict]:
    """
    检查每条明细是否超过所属费用类别的单次报销限额
        口径：单条明细金额 > categories.max_amount（max_amount 为空表示该类别不限额）
        提示而非拦截：超限不代表不能提交，最终能否全额报销由审批人判断。
        :param db: 数据库会话
        :param expense: 报销单
        :param rule: 规则配置
        :return: 命中列表
    """
    # 1.取这条规则配置的级别（规则 severity = warn/review/block）
    hits = []                                       # 命中列表
    level = _rule_level(rule)                       # 规则级别
    # 2.逐条明细比对"明细金额 > 所属类别单次限额"
    for item in expense.items:
        category = item.category                    # 明细所属类别
        # 2.1 没有类别或该类别不限额，跳过
        if category is None or category.max_amount is None:
            continue
        # 2.2 超过限额就记一条命中（提示而非拦截）
        if Decimal(str(item.amount)) > Decimal(str(category.max_amount)):
            hits.append(_build_hit(
                rule,                   # 命中的规则
                level,                  # 提示级别（warn/review/block）
                f"明细「{item.description}」申请金额 {item.amount} 元，超过「{category.name}」"
                f"单次报销限额 {category.max_amount} 元，可能无法全额报销",   # 提示文案
                {                                               # 命中证据
                    "item_id": item.id,                             # 明细ID
                    "item_description": item.description,           # 明细说明（定位风险落在哪条明细）
                    "amount": str(item.amount),                     # 实际申请金额
                    "max_amount": str(category.max_amount),         # 类别单次限额
                    "category_id": category.id,                     # 费用类别ID
                    "category_name": category.name,                 # 费用类别名称
                },
            ))
    return hits



# 规则2：发票缺失
def _check_invoice_missing(db: Session, expense: Expense, rule: Rule) -> list[dict]:
    """
    检查明细是否"既没有发票、也没有填写无票说明"
        判定顺序：
            1. 有附件（未软删）→ 视为有票，跳过；
            2. 有发票号 → 视为有票，跳过；
            3. 填写了无票说明 → 允许无票，跳过；
            4. 以上都没有 → 提示"未上传发票且未填写无票说明"。
        :param db: 数据库会话
        :param expense: 报销单
        :param rule: 规则配置
        :return: 命中列表
    """
    hits = []                                       # 命中列表
    level = _rule_level(rule)                       # 规则级别
    # 1.逐条明细判断"有没有票、有没有无票说明"
    for item in expense.items:
        # 1.1 有未软删的附件 → 视为有票
        has_attachment = any(not attachment.is_deleted for attachment in item.attachments)   # 是否有发票附件
        if has_attachment or item.invoice_no:
            continue
        # 1.2 填了无票说明 → 允许无票
        if (item.no_invoice_reason or "").strip():
            continue
        # 1.3 两者都没有 → 命中"发票缺失"
        hits.append(_build_hit(
            rule,                       # 命中的规则
            level,                      # 提示级别（warn/review/block）
            f"明细「{item.description}」（{item.amount} 元）未上传发票，也未填写无票说明",   # 提示文案
            {                                               # 命中证据
                "item_id": item.id,                         # 明细ID
                "item_description": item.description,       # 明细说明
                "amount": str(item.amount),                 # 明细金额
                "attachments": 0,                           # 未软删附件数（此处恒为 0）
                "invoice_no": None,                         # 发票号（此处为空）
            },
        ))
    return hits



# 规则3：重复报销
def _check_duplicate(db: Session, expense: Expense, rule: Rule) -> list[dict]:
    """
    检查同人 + 同费用日期 + 同类别 + 同金额的重复明细
        口径（按约定）：
            - 只比对"费用发生日期在窗口期内"的明细，窗口默认 60 天；
            - 金额相同但类别不同 **不算** 重复；
            - 同一张单内的两条明细互相比对也算（一次提交里重复填两行同样费用）；
            - 已取消/已驳回的历史单据不参与比对（它们没真正报销）。
        提示而非拦截：同日同额可能是两次真实消费，交由审批人确认。
        :param db: 数据库会话
        :param expense: 报销单
        :param rule: 规则配置
        :return: 命中列表
    """
    config = rule.config or {}                      # 规则配置
    window_days = int(config.get("window_days", 60) or 60)   # 重复窗口天数
    earliest = date.today() - timedelta(days=window_days)   # 窗口起始日
    level = _rule_level(rule)                       # 规则级别

    # 1.先筛出窗口内的当前明细；没有候选直接返回，避免无谓查询
    current_items = [
        item for item in expense.items
        if item.category_id is not None and item.expense_date >= earliest
    ]
    if not current_items:
        return []

    # 2.一次 join 查出所有历史候选明细（不再"每条当前明细查一遍历史单据"）
    rows = (
        db.query(
            Expense.id,
            Expense.expense_no,
            ExpenseItem.id,
            ExpenseItem.category_id,
            ExpenseItem.amount,
            ExpenseItem.expense_date,
        )
        .join(ExpenseItem, ExpenseItem.expense_id == Expense.id)
        .filter(
            Expense.user_id == expense.user_id,          # 同一提交人
            Expense.id != expense.id,                    # 排除本单
            Expense.status.notin_([
                ExpenseStatus.DRAFT,                      # 草稿未进入报销，不算已报过
                ExpenseStatus.CANCELLED,
                ExpenseStatus.REJECTED,
            ]),
            ExpenseItem.expense_date >= earliest,        # 只取窗口内明细
        )
        .order_by(Expense.id.asc(), ExpenseItem.id.asc())
        .all()
    )

    # 3.按（类别、金额、费用日期）建索引，匹配时 O(1) 取历史候选
    historical_map: dict[tuple, list[dict]] = {}
    for expense_id, expense_no, item_id, category_id, amount, expense_date in rows:
        key = (category_id, amount, expense_date)
        historical_map.setdefault(key, []).append({
            "expense_id": expense_id,
            "expense_no": expense_no,
            "item_id": item_id,
        })

    # 4.当前单据内部也按同一个 key 分组，先处理"同单内重复"
    current_map: dict[tuple, list] = {}
    for item in current_items:
        current_map.setdefault((item.category_id, item.amount, item.expense_date), []).append(item)

    hits = []                                       # 命中列表
    for item in current_items:
        key = (item.category_id, item.amount, item.expense_date)
        matches = [
            {
                "expense_id": expense.id,
                "expense_no": expense.expense_no,
                "item_id": other.id,
            }
            for other in current_map.get(key, [])
            if other.id != item.id
        ]
        matches.extend(historical_map.get(key, []))

        if not matches:
            continue

        first = matches[0]                          # 第一条匹配
        hits.append(_build_hit(
            rule,                       # 命中的规则
            level,                      # 提示级别（warn/review/block）
            f"明细「{item.description}」（{item.expense_date}，{item.amount} 元）"
            f"与单据 {first['expense_no']} 中的同类费用重复，请确认是否重复报销",   # 提示文案
            {                                               # 命中证据
                "item_id": item.id,                         # 明细ID
                "item_description": item.description,       # 明细说明
                "amount": str(item.amount),                 # 明细金额
                "expense_date": item.expense_date.isoformat(),  # 费用发生日期
                "window_days": window_days,                 # 重复比对窗口（天）
                "matches": matches[:3],                     # 前 3 条重复证据
                "match_count": len(matches),                # 重复项总数
            },
        ))
    return hits



# 规则4：跨期报销
def _check_period_late(db: Session, expense: Expense, rule: Rule) -> list[dict]:
    """
    检查费用发生日期距提交日期是否超过 N 天
        基准日：优先用单据提交时间（提交时已写入），草稿预检时用今天。
        :param db: 数据库会话
        :param expense: 报销单
        :param rule: 规则配置
        :return: 命中列表
    """
    # 1.读配置：超期天数阈值 + 基准日（提交时用提交日，预检时用今天）
    config = rule.config or {}                      # 规则配置
    max_days = int(config.get("max_days", 60) or 60)   # 允许最大天数
    base_date = expense.submitted_at.date() if expense.submitted_at else date.today()   # 基准日期
    level = _rule_level(rule)                       # 规则级别

    # 2.逐条明细计算"费用发生日距基准日多少天"
    hits = []                                       # 命中列表
    for item in expense.items:
        late_days = (base_date - item.expense_date).days   # 超期天数
        # 3.超过阈值就记一条命中（带实际天数与阈值，审批人可核对）
        if late_days > max_days:
            hits.append(_build_hit(
                rule,                   # 命中的规则
                level,                  # 提示级别（warn/review/block）
                f"明细「{item.description}」费用发生日期 {item.expense_date}，"
                f"距提交超过 {max_days} 天（实际 {late_days} 天），属于跨期报销",   # 提示文案
                {                                               # 命中证据
                    "item_id": item.id,                         # 明细ID
                    "item_description": item.description,       # 明细说明
                    "expense_date": item.expense_date.isoformat(),  # 费用发生日期
                    "late_days": late_days,                     # 实际超期天数
                    "max_days": max_days,                       # 允许的最大天数
                },
            ))
    return hits



# 规则类型 → 检查函数
CHECK_HANDLERS = {                              # 规则类型→检查函数
    "check_category_limit": _check_category_limit,
    "check_invoice_missing": _check_invoice_missing,
    "check_duplicate": _check_duplicate,
    "check_period_late": _check_period_late,
}



# 执行全部启用的检查规则
def run_rule_checks(db: Session, expense: Expense) -> list[dict]:
    """
    对一张报销单执行全部启用的检查型规则（只计算，不写库）
        单条规则异常只记日志并跳过：规则配置错误不能导致整单提交失败。
        :param db: 数据库会话
        :param expense: 报销单（需已持久化，且 items 可访问）
        :return: 命中结果列表（按规则ID、明细顺序）
    """
    # 1.取所有启用中的检查型规则，按ID升序保证执行顺序稳定
    rules = db.query(Rule).filter(                  # 启用规则查询
        Rule.rule_type.in_(CHECK_RULE_TYPES),       # 检查型规则
        Rule.is_active == True,                     # 仅启用
    ).order_by(Rule.id.asc()).all()

    # 2.逐条执行：单条规则异常只记日志并跳过，不能拖垮整单提交
    hits = []                                       # 命中列表
    for rule in rules:
        handler = CHECK_HANDLERS.get(rule.rule_type)   # 规则处理函数
        if handler is None:
            continue
        try:
            hits.extend(handler(db, expense, rule))
        except Exception:
            logger.exception(
                "规则执行失败 rule_id=%s rule_type=%s expense_id=%s（已跳过该规则）",
                rule.id, rule.rule_type, expense.id,   # 规则ID、类型、单据ID
            )
    # 3.返回全部命中（未写库，由调用方决定是否保存）
    return hits



# 汇总命中级别
def summarize_level(hits: list[dict]) -> str:
    """
    汇总一次检查的整体级别
        :param hits: 命中结果列表
        :return: ok（无命中）/ warn / review / block（取最高）
    """
    # 1.没有命中时就是正常状态
    level = "ok"                                    # 汇总级别
    # 2.逐条比较，取级别更高的一条（warn < review < block）
    for hit in hits:
        if LEVEL_ORDER[hit["level"]] > LEVEL_ORDER.get(level, 0):
            level = hit["level"]                    # 更新汇总级别
    # 3.返回汇总级别
    return level



# 保存命中结果
def save_rule_hits(db: Session, expense: Expense, hits: list[dict]) -> list[ExpenseRuleHit]:
    """
    用最新的命中结果替换库里的旧记录（不提交事务，由调用方 commit）
        :param db: 数据库会话
        :param expense: 报销单
        :param hits: run_rule_checks 的结果
        :return: 新建的命中记录列表
    """
    # 1.先清空这单上一轮的命中，保证"一单一份最新结果"
    db.query(ExpenseRuleHit).filter(ExpenseRuleHit.expense_id == expense.id).delete()

    # 2.把本次命中结果转成 ORM 记录（规则名/类型做快照，规则被删也能读）
    records = [                                     # 命中记录列表
        ExpenseRuleHit(
            expense_id=expense.id,                  # 所属报销单ID
            rule_id=hit["rule_id"],                 # 规则ID
            rule_type=hit["rule_type"],             # 规则类型快照
            rule_name=hit["rule_name"],             # 规则名称快照
            clause_id=hit.get("clause_id"),         # 制度条款快照
            level=hit["level"],                     # 命中级别（warn/review/block）
            message=hit["message"],                 # 给审批人看的提示
            evidence=hit["evidence"],               # 命中证据（金额/阈值/关联单据）
        )
        for hit in hits
    ]                                               # 命中记录列表
    # 3.批量写入会话（flush 拿到ID，事务由调用方提交）
    if records:
        db.add_all(records)
    db.flush()
    # 4.返回新建的记录
    return records
