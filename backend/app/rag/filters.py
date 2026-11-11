"""
制度条款适用性过滤（BM25 与向量检索共用）
    为什么单独抽出来：
        - 条款的 applies_to（费用大类/角色/规则类型/金额区间）与检索算法无关；
        - BM25 和 Chroma 实现都要先过滤再排序，抽出来避免两处逻辑漂移。
    边界：纯计算，不读库、不调用模型。
"""
from decimal import Decimal

from app.rag.schema import PolicyClause, PolicyQuery



# 判断条款是否适用于本次查询
def clause_applies(clause: PolicyClause, query: PolicyQuery) -> bool:
    """
    判断一条条款是否适用于本次查询
        :param clause: 制度条款
        :param query: 检索条件
        :return: 适用返回 True
    """
    # 1.费用大类：条款配置了范围时，必须与单据涉及的大类有交集
    if clause.expense_types and not (set(clause.expense_types) & set(query.expense_types)):
        return False

    # 2.角色：条款配置了范围时，提交人角色必须命中
    if clause.roles and query.role not in clause.roles:
        return False

    # 3.规则类型：条款配置了范围时，必须与本次命中规则有交集
    if clause.rule_types and not (set(clause.rule_types) & set(query.rule_types)):
        return False

    # 4.金额区间：按单据总额过滤
    total: Decimal = query.total_amount
    if clause.min_amount is not None and total < clause.min_amount:
        return False
    if clause.max_amount is not None and total > clause.max_amount:
        return False
    return True
