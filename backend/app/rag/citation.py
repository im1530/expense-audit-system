"""
制度引用校验（模型输出 → 可审计的引用列表）
    校验原则（严格模式）：
        1. clause_id 必须来自本轮实际注入 Prompt 的条款，不能引用未检索到的条款；
        2. quote 必须是该条款原文的连续片段（只忽略空白/全半角/大小写，不改写文字）；
        3. doc / chapter 一律由服务端按条款回填，不信任模型自报的出处；
        4. 非法引用只丢该条，不影响整次预审结论；
        5. 所有丢弃原因进入审计统计，供 Phase D 评测召回与幻觉率。
"""
from dataclasses import dataclass, field

from app.rag.schema import PolicyBundle
from app.rag.tokenizer import normalize_for_match, strip_wrapping_quotes



# 一次引用校验的结果
@dataclass(frozen=True)
class CitationResult:
    """
    引用校验结果
        :ivar citations: 通过校验的引用（可直接落库/返回前端）
        :ivar stats: 审计统计（检索条款、丢弃原因、未验证的 policy finding 数）
    """
    citations: list[dict] = field(default_factory=list)   # 有效引用
    stats: dict = field(default_factory=dict)             # 审计统计



# 兼容 dict / Pydantic 模型两种输入
def _value(item, key: str):
    """
    从字典或对象中取字段值
        :param item: 引用草稿
        :param key: 字段名
        :return: 字段值；不存在时返回 None
    """
    if isinstance(item, dict):
        return item.get(key)                            # 字典取值
    return getattr(item, key, None)                     # 对象取值



# 校验引用
def validate_citations(
    drafts,
    bundle: PolicyBundle | None,
    *,
    max_citations: int = 5,
    min_quote_chars: int = 8,
) -> CitationResult:
    """
    校验模型返回的制度引用
        :param drafts: 模型返回的引用草稿（dict / Pydantic 模型均可）
        :param bundle: 本轮实际注入 Prompt 的制度结果；None 表示无制度上下文
        :param max_citations: 最多保留多少条有效引用
        :param min_quote_chars: 引用原文的最小长度（去空白后）
        :return: CitationResult
    """
    drafts = list(drafts or [])                         # 统一成列表
    dropped: list[dict] = []                            # 被丢弃的引用及原因
    citations: list[dict] = []                          # 有效引用
    seen: set[str] = set()                              # 已引用的条款编号

    # 1.没有制度上下文：所有引用都不可信
    if bundle is None or bundle.is_empty:
        for draft in drafts:
            clause_id = str(_value(draft, "clause_id") or "").strip()   # 引用的条款号
            dropped.append({"clause_id": clause_id, "reason": "no_policy"})   # 记录丢弃原因
        return CitationResult(
            citations=[],
            stats={
                "policy_version": bundle.version if bundle else "",   # 制度版本
                "retrieved_clause_ids": list(bundle.clause_ids) if bundle else [],   # 检索到的条款
                "citation_count": 0,                                  # 有效引用数
                "dropped": dropped,                                   # 丢弃明细
            },
        )

    # 2.建立"本轮允许引用"的条款映射
    allowed = {hit.clause.clause_id: hit.clause for hit in bundle.hits}   # 条款号 → 条款

    # 3.逐条校验
    for draft in drafts:
        clause_id = str(_value(draft, "clause_id") or "").strip()   # 引用的条款号
        quote = str(_value(draft, "quote") or "").strip()            # 引用的原文

        # 3.1 条款号必须存在且在本轮检索集合内
        if not clause_id:
            dropped.append({"clause_id": "", "reason": "missing_clause_id"})
            continue
        clause = allowed.get(clause_id)                             # 对应条款
        if clause is None:
            dropped.append({"clause_id": clause_id, "reason": "not_retrieved"})
            continue

        # 3.2 引用原文必须存在、足够长
        quote = strip_wrapping_quotes(quote)                        # 去掉外层引号
        if not quote:
            dropped.append({"clause_id": clause_id, "reason": "missing_quote"})
            continue
        normalized_quote = normalize_for_match(quote)               # 去空白后比对
        if len(normalized_quote) < max(1, int(min_quote_chars)):
            dropped.append({"clause_id": clause_id, "reason": "quote_too_short"})
            continue

        # 3.3 必须是条款原文的连续子串（严格逐字）
        if normalized_quote not in normalize_for_match(clause.text):
            dropped.append({"clause_id": clause_id, "reason": "quote_not_found"})
            continue

        # 3.4 去重与数量上限
        if clause_id in seen:
            dropped.append({"clause_id": clause_id, "reason": "duplicate"})
            continue
        if len(citations) >= max(0, int(max_citations)):
            dropped.append({"clause_id": clause_id, "reason": "over_limit"})
            continue
        seen.add(clause_id)                                         # 标记已引用

        # 3.5 doc/chapter 由服务端回填，不采用模型自报
        citations.append({
            "clause_id": clause_id,                                 # 条款编号
            "doc": bundle.doc_title,                                # 制度名称
            "chapter": clause.section,                              # 章节
            "quote": quote,                                         # 原文摘录
        })

    # 4.返回有效引用 + 审计统计
    return CitationResult(
        citations=citations,
        stats={
            "policy_version": bundle.version,                       # 制度版本
            "retrieved_clause_ids": list(bundle.clause_ids),        # 本轮检索到的条款
            "citation_count": len(citations),                       # 有效引用数
            "dropped": dropped,                                     # 丢弃明细
        },
    )
