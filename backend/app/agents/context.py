"""
AI 预审上下文构造（数据库 → 模型可读 JSON）
    职责：
        1. 一次性加载单据、明细、类别、附件、规则命中（避免 N+1）；
        2. 只保留审核需要的信息，去掉手机号/邮箱/用户ID等无关隐私；
        3. 对文本做清洗（控制字符、Prompt 边界字样）与截断；
        4. 输出稳定、可测试的 JSON 结构，供 Prompt 与审计复用。
    边界：
        - 本模块只读数据库，不写库、不调用 LLM；
        - 状态校验在这里做（仅待审批单据允许预审），权限校验在 API 层做。
"""
import re
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.models import Department, Expense, ExpenseItem
from app.models.expense import ExpenseStatus
from app.services.rule_risk_service import RULE_TYPE_LABELS



# 单条明细说明的最大字符数（防止一条超长说明吃掉上下文）
MAX_DESCRIPTION_CHARS = 300

# 标题最大字符数
MAX_TITLE_CHARS = 200

# 规则命中最多送入条数
MAX_RULE_HITS = 20

# 控制字符（保留换行/制表符之外的不可见字符）
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")



class ContextError(Exception):
    """
    上下文构造失败（单据不存在/状态不允许/数据异常）
        由上层转成业务错误或直接标记预审失败，不把空上下文交给模型。
    """
    def __init__(self, message: str):
        """
        :param message: 错误说明
        """
        super().__init__(message)
        self.message = message      # 错误说明



# 清洗文本：去掉控制字符、防止 Prompt 边界逃逸、按长度截断
def clean_text(value: Any, limit: int) -> str:
    """
    清洗一个用户可控的文本字段
        处理顺序：
            1. None → 空串；
            2. 去掉控制字符；
            3. 把 <expense_data> 边界字样换成全角，防止用户"闭合"数据区；
            4. 去首尾空白并按 limit 截断（超出加省略号）。
        :param value: 原始值
        :param limit: 最大字符数
        :return: 清洗后的文本
    """
    if value is None:
        return ""
    text = CONTROL_CHARS.sub("", str(value))                        # 去控制字符
    text = text.replace("<expense_data>", "＜expense_data＞")       # 防止闭合数据区
    text = text.replace("</expense_data>", "＜/expense_data＞")     # 防止闭合数据区
    text = text.replace("<policy_context>", "＜policy_context＞")   # 防止伪造制度区
    text = text.replace("</policy_context>", "＜/policy_context＞") # 防止闭合制度区
    text = text.strip()                                             # 去首尾空白
    if len(text) > limit:
        text = text[:limit] + "…"                                  # 截断标记
    return text



# 金额统一格式化为两位小数字符串
def format_money(value: Any) -> str:
    """
    把金额转成两位小数字符串，避免浮点误差
        :param value: Decimal / int / str
        :return: 形如 "2500.00"
    """
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"



# 构造一次预审的上下文
def build_review_context(db: Session, expense_id: int) -> dict:
    """
    加载并构造 AI 预审上下文
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: 可直接 JSON 序列化的上下文字典
        :raises ContextError: 单据不存在、状态不允许
    """
    # 1.一次性加载主单、提交人、明细、类别、附件、规则命中
    expense = (
        db.query(Expense)
        .options(
            joinedload(Expense.user),                                       # 提交人（取角色/部门）
            selectinload(Expense.items).selectinload(ExpenseItem.category),  # 明细+类别
            selectinload(Expense.items).selectinload(ExpenseItem.attachments),  # 明细+附件
            selectinload(Expense.rule_hits),                                # 规则命中
        )
        .filter(Expense.id == expense_id)
        .first()
    )

    # 2.不存在直接报错
    if expense is None:
        raise ContextError("报销单不存在")

    # 3.仅待审批状态允许预审（草稿还没提交、已支付/撤销没有审核意义）
    if expense.status != ExpenseStatus.PENDING:
        raise ContextError("仅待审批状态的报销单可以执行 AI 预审")

    # 4.查部门名称（只取名称，不把部门ID暴露给模型）
    department_name = None
    if expense.user is not None and expense.user.department_id is not None:
        department_name = (
            db.query(Department.name)
            .filter(Department.id == expense.user.department_id)
            .scalar()
        )

    # 5.组装主单信息（不含姓名/手机号/邮箱等隐私字段）
    expense_payload = {
        "expense_no": clean_text(expense.expense_no, 50),                       # 单号
        "title": clean_text(expense.title, MAX_TITLE_CHARS),                    # 标题
        "total_amount": format_money(expense.total_amount),                     # 总金额
        "status": expense.status.value,                                         # 单据状态
        "submitter_role": (
            expense.user.role.value if hasattr(expense.user.role, "value") else expense.user.role
        ),                                                                      # 提交人角色
        "department": clean_text(department_name, 50),                          # 部门名称
        "current_level": expense.current_level,                                 # 当前审批层级
        "submitted_at": expense.submitted_at.isoformat() if expense.submitted_at else None,  # 提交时间
    }

    # 6.组装明细：按ID排序保证稳定，再按上限截断
    items = sorted(expense.items, key=lambda item: item.id)
    max_items = max(1, int(settings.AI_REVIEW_MAX_INPUT_ITEMS))
    visible_items = items[:max_items]
    item_payload = []
    for item in visible_items:
        item_payload.append({
            "item_id": item.id,                                                         # 明细ID（凭证比对/审计用）
            "category": clean_text(item.category.name if item.category else None, 50),  # 类别名称
            "expense_type": (
                item.category.expense_type.value
                if item.category is not None and hasattr(item.category.expense_type, "value")
                else (item.category.expense_type if item.category else "")
            ),                                                                          # 费用大类（制度过滤用）
            "description": clean_text(item.description, MAX_DESCRIPTION_CHARS),         # 费用说明
            "amount": format_money(item.amount),                                        # 明细金额
            "expense_date": item.expense_date.isoformat() if item.expense_date else None,  # 发生日期
            "has_invoice": any(not attachment.is_deleted for attachment in item.attachments),  # 是否有票
            "no_invoice_reason": clean_text(item.no_invoice_reason, 200),               # 无票说明
        })

    # 7.组装规则命中：按ID排序，限制条数；保留规则级别与提示
    rule_hits = sorted(expense.rule_hits, key=lambda hit: hit.id)
    visible_hits = rule_hits[:MAX_RULE_HITS]
    hit_payload = []
    for hit in visible_hits:
        hit_payload.append({
            "rule_type": hit.rule_type,                                             # 规则类型
            "rule_label": RULE_TYPE_LABELS.get(hit.rule_type, hit.rule_type),       # 中文短标签
            "rule_name": clean_text(hit.rule_name, 100),                            # 规则名称
            "clause_id": hit.clause_id,                                             # 制度条款ID
            "level": hit.level,                                                     # warn/review/block
            "message": clean_text(hit.message, 500),                                # 提示文案
        })

    # 8.返回稳定结构（truncated 明确告诉模型"还有内容未展示"）
    return {
        "expense": expense_payload,
        "items": item_payload,
        "rule_hits": hit_payload,
        "truncated": {
            "items": max(0, len(items) - len(visible_items)),
            "rule_hits": max(0, len(rule_hits) - len(visible_hits)),
        },
    }
