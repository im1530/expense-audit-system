"""
制度检索器（Phase B：纯 Python BM25 + 适用条件硬过滤）
    为什么是 BM25：
        - 当前环境没有 numpy / chromadb / embedding 模型，BM25 只用标准库即可；
        - 制度条款短、关键词明确，BM25 的可解释性与稳定性足够；
        - 通过 PolicyRetriever 协议隔离实现，后续可替换为 Chroma + Embedding。
    检索顺序：
        1. 按 applies_to 硬过滤（费用大类/角色/规则类型/金额区间）；
        2. 对剩余条款做 BM25 打分；
        3. 绝对分 + 相对分双阈值过滤；
        4. 按分数降序、条款编号升序取 Top-K，保证结果可复现。
"""
import logging
import math
from collections import Counter
from typing import Protocol

from app.config import settings
from app.rag.filters import clause_applies
from app.rag.loader import get_policy_document
from app.rag.schema import PolicyClause, PolicyDocument, PolicyHit, PolicyQuery
from app.rag.tokenizer import expand_tokens, tokenize



logger = logging.getLogger(__name__)

# BM25 参数：制度条款较短，使用常见默认值
K1 = 1.5        # 词频饱和参数
B = 0.75        # 文档长度归一化参数

# 条款关键词在文档侧重复次数（提高关键词权重）
KEYWORD_WEIGHT = 3

# 章节标题在文档侧重复次数（给条款补充上下文）
SECTION_WEIGHT = 2



# 检索器协议：未来 Chroma/Embedding 实现同一接口
class PolicyRetriever(Protocol):
    """
    制度检索器协议
        业务层只依赖本协议，不关心底层是 BM25 还是向量检索。
    """

    def retrieve(self, query: PolicyQuery) -> list[PolicyHit]:
        """
        执行一次制度检索
            :param query: 查询条件
            :return: 按相关度降序排列的命中列表
        """
        ...



# BM25 检索器
class BM25PolicyRetriever:
    """
    基于 BM25 的制度检索器
        文档侧由"关键词 + 章节标题 + 条款原文"组成；
        查询侧由报销单标题、明细说明、类别、规则标签组成。
    """

    def __init__(self, document: PolicyDocument, *, k1: float = K1, b: float = B):
        """
        :param document: 制度文档
        :param k1: 词频饱和参数
        :param b: 文档长度归一化参数
        """
        self.document = document                    # 制度文档
        self.k1 = k1                                # 词频饱和参数
        self.b = b                                  # 长度归一化参数
        self._entries: list[tuple[PolicyClause, Counter, int]] = []   # (条款, 词频, 长度)
        self._df: Counter = Counter()               # 文档频率
        self._avgdl = 0.0                           # 平均文档长度
        self._build_index()                         # 构建索引

    # 构造单条条款的文档词项
    def _clause_tokens(self, clause: PolicyClause) -> list[str]:
        """
        把一条条款转成 BM25 文档词项
            :param clause: 条款
            :return: 词项列表（关键词与章节加权）
        """
        keyword_tokens = tokenize(" ".join(clause.keywords)) * KEYWORD_WEIGHT   # 关键词加权
        section_tokens = tokenize(clause.section) * SECTION_WEIGHT             # 章节加权
        text_tokens = tokenize(clause.text)                                    # 正文词项
        return keyword_tokens + section_tokens + text_tokens                    # 合并

    # 构建倒排统计
    def _build_index(self) -> None:
        """
        构建文档频率、平均长度与每条条款的词频
            :return: 无
        """
        total_length = 0                            # 总词数
        for clause in self.document.clauses:
            tokens = self._clause_tokens(clause)    # 文档词项
            counts = Counter(tokens)                # 词频
            self._entries.append((clause, counts, len(tokens)))   # 记录条目
            total_length += len(tokens)             # 累计长度
            for term in counts:
                self._df[term] += 1                 # 文档频率 +1
        self._avgdl = total_length / len(self._entries) if self._entries else 0.0   # 平均长度

    # 适用条件过滤
    def _passes_filters(self, clause: PolicyClause, query: PolicyQuery) -> bool:
        """
        判断条款是否适用于本次查询
            :param clause: 条款
            :param query: 查询条件
            :return: 适用返回 True
        """
        return clause_applies(clause, query)                    # 与向量检索共用同一套过滤

    # 计算 BM25 分数
    def _score(self, counts: Counter, length: int, query_terms: list[str]) -> tuple[float, list[str]]:
        """
        计算一条文档相对查询的 BM25 分数
            :param counts: 文档词频
            :param length: 文档长度
            :param query_terms: 去重后的查询词
            :return: (分数, 命中词列表)
        """
        score = 0.0                                 # 累计分数
        matched: list[str] = []                     # 命中词
        for term in query_terms:
            frequency = counts.get(term, 0)         # 词频
            if frequency <= 0:
                continue                            # 未命中
            document_frequency = self._df.get(term, 0)   # 文档频率
            idf = math.log(1.0 + (len(self._entries) - document_frequency + 0.5) / (document_frequency + 0.5))   # IDF
            denominator = frequency + self.k1 * (1.0 - self.b + self.b * length / self._avgdl)   # 归一化分母
            score += idf * (frequency * (self.k1 + 1.0)) / denominator   # BM25 项
            matched.append(term)                    # 记录命中词
        return score, matched

    # 执行检索
    def retrieve(self, query: PolicyQuery) -> list[PolicyHit]:
        """
        执行一次制度检索
            :param query: 查询条件
            :return: 命中列表（分数降序、编号升序）
        """
        # 1.空查询或空索引直接返回
        query_terms = list(dict.fromkeys(expand_tokens(tokenize(query.text))))   # 去重后的查询词
        if not query_terms or not self._entries or self._avgdl <= 0:
            return []

        # 2.逐条过滤并打分
        candidates: list[PolicyHit] = []            # 候选命中
        for clause, counts, length in self._entries:
            if not self._passes_filters(clause, query):
                continue                            # 不适用，跳过
            score, matched = self._score(counts, length, query_terms)   # BM25 分数
            if score <= 0:
                continue                            # 毫无匹配，跳过
            candidates.append(PolicyHit(
                clause=clause,
                score=round(score, 4),
                matched_terms=tuple(matched[:8]),
            ))

        # 3.排序：分数降序，同分按编号升序，保证可复现
        candidates.sort(key=lambda hit: (-hit.score, hit.clause.clause_id))
        if not candidates:
            return []

        # 4.双阈值过滤：绝对下限 + 相对最高分比例
        top_score = candidates[0].score             # 最高分
        cutoff = max(settings.POLICY_MIN_SCORE, top_score * settings.POLICY_MIN_RELATIVE_SCORE)   # 截断线
        selected = [hit for hit in candidates if hit.score >= cutoff]   # 达标命中

        # 5.截断 Top-K
        top_k = max(1, min(20, int(settings.POLICY_TOP_K)))   # 安全范围
        return selected[:top_k]



# 进程级检索器缓存
_retriever: PolicyRetriever | None = None



# 按配置创建检索器
def build_retriever(document: PolicyDocument) -> PolicyRetriever:
    """
    根据 POLICY_RETRIEVER 创建检索器；Chroma 不可用时降级 BM25
        :param document: 制度文档
        :return: 检索器（BM25 或 Chroma）
    """
    # 1.向量/混合后端：延迟导入，失败自动降级（缺依赖/模型缺失/建库异常）
    backend = str(settings.POLICY_RETRIEVER).lower()
    if backend in ("chroma", "hybrid"):
        try:
            if backend == "hybrid":
                from app.rag.hybrid_retriever import HybridPolicyRetriever

                return HybridPolicyRetriever(
                    document,
                    keyword=BM25PolicyRetriever(
                        document,
                        k1=float(settings.BM25_K1),
                        b=float(settings.BM25_B),
                    ),
                )

            from app.rag.chroma_retriever import ChromaPolicyRetriever

            return ChromaPolicyRetriever(document)
        except Exception:
            logger.warning("%s 检索初始化失败，本次降级为 BM25", backend, exc_info=True)

    # 2.默认 BM25（零依赖，保证预审不被检索后端拖垮）
    return BM25PolicyRetriever(
        document,
        k1=float(settings.BM25_K1),
        b=float(settings.BM25_B),
    )



# 获取检索器
def get_retriever() -> PolicyRetriever:
    """
    获取进程级缓存的检索器
        文档热更新后（对象身份变化）会自动重建索引。
        :return: 当前配置对应的检索器
    """
    global _retriever
    document = get_policy_document()                # 当前制度文档
    if _retriever is None or _retriever.document is not document:
        _retriever = build_retriever(document)      # 重建索引
        logger.info(
            "制度检索索引构建完成 backend=%s clauses=%s",
            type(_retriever).__name__,
            len(document.clauses),
        )
    return _retriever



# 清空检索器缓存（测试/热更新用）
def reset_retriever_cache() -> None:
    """
    清空进程级检索器缓存
        :return: 无
    """
    global _retriever
    _retriever = None
