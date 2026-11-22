"""
规则风险标记服务（把规则命中结果翻译成审批人看得懂的"风险标记"）
    背景：规则引擎（services/rule_engine.py）只负责算出"命中了哪条规则、证据是什么"，
    审批人真正需要的是"这单有什么风险、风险出在哪条明细、严重到什么程度"。
    本模块承担这层翻译，供审批待办 / 已办列表与管理端命中率统计复用：
        - build_risk_marks：把命中记录转成结构化风险标记（规则中文名 + 级别 + 具体提示 + 关联明细）；
        - load_risk_map：按一批报销单批量加载命中（一次 SQL），并汇总每单的整体风险级别与风险标签；
        - attach_risk_marks：把汇总结果挂到 ORM 对象上，供 PendingApprovalItem 直接序列化；
        - get_rule_hit_stats：按规则统计命中次数、涉及单据数与命中率，供管理端调阈值。
    设计约定：
        - 只读：本模块不写库、不提交事务；
        - 文案分层：rule_label 是短标签（列表徽标用），message 是具体到明细/金额的完整提示（悬停/展开看）；
        - 级别取最高：一单命中多条规则时，整体级别取 warn < review < block 中最严重的一条；
        - 命名避开 AI 风险字段：接口统一加 rule_ 前缀（rule_risk_level 等），
          与 expenses.risk_level（AI 风险评估结果）区分，避免两者混淆。
"""
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import Expense, ExpenseRuleHit, Rule
from app.services.rule_engine import CHECK_RULE_TYPES, LEVEL_ORDER, summarize_level



# 规则类型 → 列表短标签（前端徽标直接展示，一行内看得懂"是什么风险"）
RULE_TYPE_LABELS = {                            # 规则类型→短标签
    "check_category_limit": "明细超类别限额",
    "check_invoice_missing": "发票缺失",
    "check_duplicate": "疑似重复报销",
    "check_period_late": "跨期报销",
    "check_evidence_strength": "凭证强度不足",
    "amount": "审批流匹配",
}

# 规则类型 → 一句话解释（前端 tooltip / 审批说明用，说明这条规则到底在查什么）
RULE_TYPE_HINTS = {                             # 规则类型→解释
    "check_category_limit": "单条明细的申请金额超过了该费用小类的单次报销限额",
    "check_invoice_missing": "明细既没有发票附件、也没有发票号，且未填写无票说明",
    "check_duplicate": "同一提交人、同一费用日期、同一类别、同一金额的费用在窗口期内重复出现",
    "check_period_late": "费用发生日期距提交日期过久，超过公司规定的报销时效",
    "check_evidence_strength": "支付截图等弱凭证金额超过免复核阈值，需要人工核对",
    "amount": "按提交人角色与报销金额匹配审批流",
}

# 级别 → 中文标签（徽标配色由前端按 level 决定）
LEVEL_LABELS = {                                # 级别→中文标签
    "ok": "无风险",
    "warn": "提示",
    "review": "需复核",
    "block": "阻止提交",
}


# 构造一单"无风险"时的空汇总（每次返回新对象，避免多个单据共用同一个可变列表）
def empty_risk_summary() -> dict:
    """
    生成一份"无风险"的汇总结果（没有任何命中记录时使用）
        :return: {"level": "ok", "count": 0, "labels": [], "marks": []}
    """
    return {
        "level": "ok",      # 无风险
        "count": 0,         # 命中条数
        "labels": [],       # 风险短标签
        "marks": [],        # 完整风险标记
    }



# 把一条命中记录翻译成结构化风险标记
def build_risk_mark(hit) -> dict:
    """
    把一条规则命中记录翻译成审批人可读的风险标记
        字段说明：
            - rule_label：短标签，如"明细超类别限额"；
            - rule_hint：这条规则在查什么，用于解释"为什么提示"；
            - level / level_label：严重程度及中文名；
            - message：规则引擎写入的具体提示，已包含明细名称、金额、阈值等细节；
            - item_id / item_description：风险落在哪条明细（单据级风险为 None）。
        :param hit: ExpenseRuleHit 实体（或结构相同的对象）
        :return: 风险标记字典，可直接交给 RuleRiskMark 校验
    """
    evidence = hit.evidence or {}                   # 命中证据

    return {
        "rule_id": hit.rule_id,                                             # 规则ID（规则删除后仍保留）
        "rule_type": hit.rule_type,                                         # 规则类型
        "rule_label": RULE_TYPE_LABELS.get(hit.rule_type, hit.rule_type),   # 列表短标签
        "rule_hint": RULE_TYPE_HINTS.get(hit.rule_type, ""),                # 规则解释
        "rule_name": hit.rule_name,                                         # 规则名称快照
        "clause_id": getattr(hit, "clause_id", None),                       # 制度条款ID
        "level": hit.level,                                                 # warn/review/block
        "level_label": LEVEL_LABELS.get(hit.level, hit.level),              # 级别中文名
        "message": hit.message,                                             # 具体提示文案
        "item_id": evidence.get("item_id"),                                 # 关联明细ID
        "item_description": evidence.get("item_description"),               # 关联明细说明
        "evidence": evidence or None,                                       # 命中证据（金额/阈值/关联单据）
    }



# 批量翻译命中记录，并按严重程度排序
def build_risk_marks(hits: Iterable) -> list[dict]:
    """
    把一批命中记录翻译成风险标记列表，按"严重程度降序、规则ID升序"排序
        审批人最关心最严重的风险，因此 block 排最前，其次 review、warn。
        :param hits: 命中记录集合
        :return: 风险标记字典列表
    """
    # 1.逐条命中翻译成结构化标记
    marks = [build_risk_mark(hit) for hit in hits]   # 风险标记列表
    # 2.排序键：级别数字取负（block 最前），同级再按规则ID升序，保证输出稳定
    marks.sort(key=lambda mark: (-LEVEL_ORDER.get(mark["level"], 0), mark["rule_id"] or 0))
    # 3.返回排好序的标记列表
    return marks



# 汇总一单的风险标记
def summarize_risk_marks(marks: list[dict]) -> dict:
    """
    汇总一张报销单的风险标记（整体级别、条数、去重后的短标签）
        :param marks: build_risk_marks 的结果
        :return: {"level", "count", "labels", "marks"}
    """
    # 1.没有任何命中：返回一份独立的"无风险"汇总
    if not marks:
        return empty_risk_summary()

    # 2.短标签去重但保持"严重程度优先"的原有顺序（build_risk_marks 已排好序）
    labels = list(dict.fromkeys(mark["rule_label"] for mark in marks))   # 去重风险标签

    # 3.汇总：整体级别取最高、条数、去重标签、完整标记
    return {
        "level": summarize_level(marks),    # 取最严重的一条
        "count": len(marks),                # 命中条数
        "labels": labels,                   # 去重后的风险标签
        "marks": marks,                     # 完整标记（含具体提示）
    }



# 批量加载一批报销单的风险汇总
def load_risk_map(db: Session, expense_ids: list[int]) -> dict[int, dict]:
    """
    批量加载多张报销单的规则命中并汇总（一次 SQL，避免列表接口 N+1 查询）
        命中记录由"提交/重新提交"时写入，保证"一单一份最新结果"；
        没有命中记录的单据返回空汇总（level=ok），前端按"无风险"渲染。
        :param db: 数据库会话
        :param expense_ids: 当前页报销单ID列表
        :return: {报销单ID: 风险汇总字典}，未命中的单据也在字典里（值为空汇总）
    """
    # 1.先用"无风险"占位，保证返回的字典覆盖所有传入单据
    risk_map = {expense_id: empty_risk_summary() for expense_id in expense_ids}   # 单据ID→风险汇总
    if not expense_ids:
        return risk_map

    # 2.一次性查出这批单据的全部命中记录
    hits = (                                        # 命中记录查询
        db.query(ExpenseRuleHit)                                # 命中记录表
        .filter(ExpenseRuleHit.expense_id.in_(expense_ids))     # 只取本页单据的命中
        .order_by(ExpenseRuleHit.id.asc())                      # 按写入顺序，输出稳定
        .all()
    )

    # 3.先按单据分组，再逐单翻译，避免在循环里反复排序
    grouped: dict[int, list] = {}                   # 按单据分组
    for hit in hits:
        grouped.setdefault(hit.expense_id, []).append(hit)

    # 4.每张单汇总成 {level, count, labels, marks}
    for expense_id, expense_hits in grouped.items():
        risk_map[expense_id] = summarize_risk_marks(build_risk_marks(expense_hits))   # 汇总该单风险

    # 5.返回完整映射
    return risk_map



# 把风险汇总挂到 ORM 对象上，供 PendingApprovalItem 序列化
def attach_risk_marks(expenses: list, risk_map: dict[int, dict]) -> None:
    """
    把风险汇总写成 ORM 对象的动态属性（不落库、不改表结构）
        属性名统一加 rule_ 前缀，避免覆盖 expenses.risk_level / risk_score 这些 AI 字段：
            - rule_risk_level：ok / warn / review / block
            - rule_risk_count：命中条数
            - rule_risk_labels：去重后的风险标签
            - rule_risk_marks：完整风险标记（含具体提示与关联明细）
        :param expenses: 当前页报销单 ORM 对象列表
        :param risk_map: load_risk_map 的结果
        :return: 无（直接修改传入对象）
    """
    for expense in expenses:
        summary = risk_map.get(expense.id) or empty_risk_summary()   # 风险汇总
        expense.rule_risk_level = summary["level"]          # 整体级别：ok/warn/review/block
        expense.rule_risk_count = summary["count"]          # 命中条数
        expense.rule_risk_labels = summary["labels"]        # 去重后的风险短标签
        expense.rule_risk_marks = summary["marks"]          # 完整风险标记



# 计算命中率（保留 4 位小数，避免出现 0.33333333 这类长尾）
def _hit_rate(part: int, total: int) -> float:
    """
    计算命中率 = 命中单据数 / 已提交单据数
        :param part: 命中单据数
        :param total: 已提交单据总数
        :return: 0~1 之间的小数，保留 4 位；分母为 0 时返回 0.0
    """
    # 1.分母为 0：直接返回 0，避免除零
    if total <= 0:
        return 0.0
    # 2.计算比例并四舍五入到 4 位
    return round(part / total, 4)



# 规则命中率统计（管理端调阈值用）
def get_rule_hit_stats(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 统计起始日期
    end_date: date | None = None,                   # 统计结束日期
) -> dict:
    """
    按规则统计命中次数、涉及单据数与命中率（管理端规则治理用）
        统计口径：
            - 分母 submitted_total：统计区间内"已提交"的报销单数（submitted_at 非空，含之后被撤回/撤销的单据）；
            - 分子 expense_count：当前命中结果里，涉及该规则的报销单数（同一单命中多条只算一次）；
            - 命中率 hit_rate = expense_count / submitted_total；
            - 日期区间按报销单提交时间（submitted_at）过滤，结束日按"含当天"处理。
        为什么用"当前命中结果"：命中记录在每次提交/重新提交时整体替换，
        因此它代表每张单最近一次提交时的规则检查结果，不会重复累计历史命中。
        :param db: 数据库会话
        :param start_date: 可选，统计起始日期（按提交时间）
        :param end_date: 可选，统计结束日期（含当天）
        :return: 含整体指标与逐条规则明细的字典
    """
    # 1.取统计区间内已提交的报销单ID（分母基数）
    expense_query = db.query(Expense.id).filter(Expense.submitted_at.isnot(None))   # 已提交单据查询
    if start_date is not None:
        # 起始日含当天：submitted_at >= 起始日 00:00:00
        expense_query = expense_query.filter(Expense.submitted_at >= start_date)   # 起始日条件
    if end_date is not None:
        # 结束日含当天：用"次日 0 点之前"的半开区间，避免漏掉结束日白天的单据
        expense_query = expense_query.filter(Expense.submitted_at < end_date + timedelta(days=1))   # 结束日条件

    expense_ids = [row[0] for row in expense_query.all()]   # 只取ID列，减少数据传输
    submitted_total = len(expense_ids)              # 已提交单据数

    # 2.一次性取这些单据的命中记录（没有已提交单据时不必查）
    hits = []                                       # 命中记录
    if expense_ids:
        hits = (                                    # 命中记录查询
            db.query(ExpenseRuleHit)                                # 命中记录表
            .filter(ExpenseRuleHit.expense_id.in_(expense_ids))     # 限定统计区间的单据
            .order_by(ExpenseRuleHit.id.asc())                      # 按写入顺序，输出稳定
            .all()
        )

    # 3.按规则聚合：命中条数、涉及单据集合、各级别条数
    by_rule: dict[int | None, dict] = {}            # 按规则聚合
    for hit in hits:
        bucket = by_rule.setdefault(                # 规则聚合桶
            hit.rule_id,                            # 分组键：规则ID
            {                                       # 规则统计桶
                "rule_id": hit.rule_id,             # 规则ID
                "rule_name": hit.rule_name,         # 规则名称快照（规则删除后仍可读）
                "rule_type": hit.rule_type,         # 规则类型快照
                "hit_count": 0,                     # 命中记录条数
                "expense_ids": set(),               # 涉及的单据ID集合（去重计数用）
                "warn_count": 0,                    # warn 级命中条数
                "review_count": 0,                  # review 级命中条数
                "block_count": 0,                   # block 级命中条数
            },
        )
        bucket["hit_count"] += 1                    # 命中数加一
        bucket["expense_ids"].add(hit.expense_id)   # 记录涉及单据
        if hit.level in ("warn", "review", "block"):
            bucket[f"{hit.level}_count"] += 1       # 对应级别计数加一

    # 4.以规则表为基准输出（含从未命中的检查规则，便于发现"规则配了但没生效"）
    rules = db.query(Rule).order_by(Rule.id.asc()).all()   # 全部规则
    rule_map = {rule.id: rule for rule in rules}    # 规则ID→实体
    items: list[dict] = []                          # 统计结果列表
    covered_rule_ids: set[int | None] = set()       # 已覆盖规则ID

    for rule in rules:
        # amount 规则只参与审批流匹配，不产生检查命中，不进入命中率统计
        if rule.rule_type not in CHECK_RULE_TYPES:
            continue
        stat = by_rule.get(rule.id, {})             # 规则命中统计
        expense_count = len(stat.get("expense_ids", ()))   # 涉及单据数
        items.append({
            "rule_id": rule.id,                                             # 规则ID
            "rule_name": rule.name,                                         # 规则名称
            "rule_type": rule.rule_type,                                    # 规则类型
            "rule_label": RULE_TYPE_LABELS.get(rule.rule_type, rule.rule_type),  # 类型中文短标签
            "is_active": bool(rule.is_active),                              # 当前是否启用
            "hit_count": int(stat.get("hit_count", 0)),                     # 命中记录条数
            "expense_count": expense_count,                                 # 涉及单据数（去重）
            "warn_count": int(stat.get("warn_count", 0)),                   # warn 级命中条数
            "review_count": int(stat.get("review_count", 0)),               # review 级命中条数
            "block_count": int(stat.get("block_count", 0)),                 # block 级命中条数
            "hit_rate": _hit_rate(expense_count, submitted_total),          # 命中率
        })
        covered_rule_ids.add(rule.id)               # 标记已覆盖

    # 5.规则已被删除但仍留有历史命中：按命中记录里的名称快照补一行，避免数据"凭空消失"
    for rule_id, stat in by_rule.items():
        if rule_id in covered_rule_ids:
            continue
        expense_count = len(stat["expense_ids"])    # 涉及单据数
        items.append({
            "rule_id": rule_id,                                             # 规则ID（已删除规则的原ID）
            "rule_name": stat["rule_name"],                                 # 规则名称快照
            "rule_type": stat["rule_type"],                                 # 规则类型快照
            "rule_label": RULE_TYPE_LABELS.get(stat["rule_type"], stat["rule_type"]),  # 类型中文短标签
            "is_active": False,                                             # 规则已删除，按停用展示
            "hit_count": int(stat["hit_count"]),                            # 命中记录条数
            "expense_count": expense_count,                                 # 涉及单据数（去重）
            "warn_count": int(stat["warn_count"]),                          # warn 级命中条数
            "review_count": int(stat["review_count"]),                      # review 级命中条数
            "block_count": int(stat["block_count"]),                        # block 级命中条数
            "hit_rate": _hit_rate(expense_count, submitted_total),          # 命中率
        })

    # 6.按命中次数倒序，命中多的规则排前面；同次数按规则ID升序，保证输出稳定
    items.sort(key=lambda item: (-item["hit_count"], item["rule_id"] or 0))

    # 7.整体指标：至少命中一条规则的报销单数 / 已提交单据数
    hit_expense_ids: set[int] = set()               # 有命中的单据ID
    for stat in by_rule.values():
        hit_expense_ids |= stat["expense_ids"]      # 合并各规则涉及的单据ID（自动去重）

    return {
        "start_date": start_date,                                   # 统计起始日期（原样回显）
        "end_date": end_date,                                       # 统计结束日期（原样回显）
        "submitted_total": submitted_total,                         # 已提交单据数（命中率分母）
        "hit_expense_count": len(hit_expense_ids),                  # 至少命中一条规则的单据数
        "total_hits": len(hits),                                    # 命中记录总条数
        "overall_hit_rate": _hit_rate(len(hit_expense_ids), submitted_total),  # 整体命中率
        "items": items,                                             # 逐条规则统计
    }
