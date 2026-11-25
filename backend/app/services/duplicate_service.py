"""重复报销口径的共享常量，避免规则引擎与 Agent 工具各写一份状态列表。"""
from app.models.expense import ExpenseStatus

# 草稿未真正进入报销；取消/驳回也不是“已报过”，都不作为重复证据。
DUPLICATE_EXCLUDED_STATUSES = (
    ExpenseStatus.DRAFT,
    ExpenseStatus.CANCELLED,
    ExpenseStatus.REJECTED,
)
