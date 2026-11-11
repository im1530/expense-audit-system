"""
制度检索服务（报销上下文 → 制度结果集）
    职责：
        1. 从已脱敏的预审上下文构造 PolicyQuery；
        2. 调用检索器，把命中结果包装成 PolicyBundle；
        3. 统一降级：制度文件缺失、解析失败、检索异常一律返回空结果，不阻断预审。
    边界：
        - 不拼 Prompt、不校验引用（分别由 prompts.py / citation.py 负责）；
        - 不写数据库、不调用 LLM。
"""
import logging
from decimal import Decimal, InvalidOperation

from app.config import settings
from app.rag.retriever import get_retriever
from app.rag.schema import PolicyBundle, PolicyQuery



logger = logging.getLogger(__name__)

# 查询文本最大长度：防止超长明细说明淹没关键词
MAX_QUERY_CHARS = 2000



# 构造制度查询
def build_policy_query(context: dict) -> PolicyQuery:
    """
    从预审上下文构造制度查询条件
        :param context: build_review_context 的结果
        :return: PolicyQuery
    """
    expense = context.get("expense") or {}                  # 主单信息
    items = context.get("items") or []                      # 明细列表
    rule_hits = context.get("rule_hits") or []              # 规则命中

    # 1.拼接查询文本：标题、明细说明、类别、费用大类、规则标签
    parts: list[str] = []                                   # 查询片段
    title = str(expense.get("title") or "").strip()         # 单据标题
    if title:
        parts.append(title)                                 # 标题参与检索

    expense_types: set[str] = set()                         # 涉及的费用大类
    for item in items:
        description = str(item.get("description") or "").strip()   # 明细说明
        category = str(item.get("category") or "").strip()         # 类别名称
        expense_type = str(item.get("expense_type") or "").strip() # 费用大类
        if description:
            parts.append(description)                       # 说明参与检索
        if category:
            parts.append(category)                          # 类别参与检索
            parts.append(category)                          # 类别重复一次，提高权重
        if expense_type:
            expense_types.add(expense_type)                 # 记录费用大类
            parts.append(expense_type)                      # 大类参与检索
            parts.append(expense_type)                      # 大类重复一次

    rule_types: set[str] = set()                            # 本次命中的规则类型
    for hit in rule_hits:
        rule_label = str(hit.get("rule_label") or "").strip()   # 规则短标签
        message = str(hit.get("message") or "").strip()         # 规则提示
        rule_type = str(hit.get("rule_type") or "").strip()     # 规则类型
        if rule_label:
            parts.append(rule_label)                        # 标签参与检索
        if message:
            parts.append(message)                           # 提示参与检索
        if rule_type:
            rule_types.add(rule_type)                       # 记录规则类型

    # 1.1 凭证校验结论也参与制度检索（如"凭证强度不足"应能召回发票/凭证条款）
    for finding in context.get("document_findings") or []:
        message = str(finding.get("message") or "").strip()     # 凭证风险提示
        if message:
            parts.append(message)                           # 提示参与检索

    # 2.金额：解析失败按 0 处理，避免影响检索
    try:
        total_amount = Decimal(str(expense.get("total_amount") or "0"))   # 单据总金额
    except (InvalidOperation, ValueError):
        total_amount = Decimal("0")                         # 解析失败兜底

    # 3.角色与文本长度
    role = str(expense.get("submitter_role") or "").strip()  # 提交人角色
    text = " ".join(part for part in parts if part)[:MAX_QUERY_CHARS]   # 合并并截断

    return PolicyQuery(
        text=text,                                          # 查询文本
        expense_types=frozenset(expense_types),             # 费用大类集合
        role=role,                                          # 提交人角色
        total_amount=total_amount,                          # 单据总金额
        rule_types=frozenset(rule_types),                   # 规则类型集合
    )



# 检索制度
def retrieve_policy(context: dict) -> PolicyBundle:
    """
    为一次预审检索制度条款
        任何异常都降级为空结果，保证 AI 预审主流程不中断。
        :param context: build_review_context 的结果
        :return: PolicyBundle（无命中或异常时为空）
    """
    # 1.总开关关闭：不加载文件、不检索
    if not settings.POLICY_RAG_ENABLED:
        return PolicyBundle.empty()

    try:
        # 2.构造查询并执行检索
        retriever = get_retriever()                         # 当前检索器
        query = build_policy_query(context)                 # 查询条件
        hits = retriever.retrieve(query)                    # 命中列表
        document = retriever.document                       # 当前制度文档

        # 3.包装结果集
        bundle = PolicyBundle(
            doc_id=document.doc_id,                         # 制度标识
            doc_title=document.title,                       # 制度名称
            version=document.version,                       # 制度版本
            hits=tuple(hits),                               # 命中条款
        )
        logger.info(
            "制度检索完成 expense_no=%s policy=%s query_chars=%s hits=%s",
            context.get("expense", {}).get("expense_no"),
            document.version,
            len(query.text),
            len(bundle.hits),
        )
        return bundle

    except Exception:
        # 4.制度缺失/解析失败/检索异常：降级为无制度模式
        logger.warning("制度检索失败，本次预审降级为无制度模式", exc_info=True)
        return PolicyBundle.empty()
