"""
混合检索（BM25 优先 + 向量补齐）
    为什么需要：
        - 纯 BM25：结构化表述 100%，但口语化只有 60%；
        - 纯向量：口语化 85%，但结构化掉到 87.5%，且 Top-1 命中率明显下降；
        - 两者互补：先保留关键词结果（保证结构化的 Top-1），不足 Top-K 时用向量结果补齐。
    设计：
        - 不做分数融合（BM25 分数与余弦相似度量纲不同），只做结果补位；
        - 任一路由器失败/为空都不影响另一路，保证预审不被检索拖垮；
        - 与其它实现同一 PolicyRetriever 协议，业务层零改动。
    边界：只做检索合并，不调用 LLM、不写库。
"""
import logging

from app.config import settings
from app.rag.retriever import BM25PolicyRetriever
from app.rag.schema import PolicyDocument, PolicyHit, PolicyQuery


logger = logging.getLogger(__name__)



# 混合检索器
class HybridPolicyRetriever:
    """
    BM25 + 向量检索的 RRF 融合
        :ivar document: 制度文档
        :ivar keyword: 关键词检索器
        :ivar vector: 向量检索器
    """

    def __init__(self, document: PolicyDocument, *, keyword=None, vector=None):
        """
        :param document: 制度文档
        :param keyword: 可注入的关键词检索器（测试用）
        :param vector: 可注入的向量检索器（测试用）
        """
        # 1.保存文档
        self.document = document

        # 2.关键词检索：默认 BM25，零依赖
        self.keyword = keyword or BM25PolicyRetriever(document)

        # 3.向量检索：默认 Chroma；初始化失败直接抛出，由工厂降级
        if vector is not None:
            self.vector = vector
        else:
            from app.rag.chroma_retriever import ChromaPolicyRetriever

            self.vector = ChromaPolicyRetriever(document)

    # 执行检索
    def retrieve(self, query: PolicyQuery) -> list[PolicyHit]:
        """
        关键词优先、向量补齐：BM25 命中不足 Top-K 时用向量结果补位
            :param query: 查询条件
            :return: 命中列表（关键词顺序在前，向量补位在后）
        """
        # 1.两路召回：单路异常不影响另一路
        keyword_hits = self._safe(self.keyword, query)
        vector_hits = self._safe(self.vector, query)

        # 2.关键词结果优先保留（结构化表述靠它保证 Top-1）
        top_k = max(1, int(settings.POLICY_TOP_K))
        merged: list[PolicyHit] = list(keyword_hits[:top_k])
        seen = {hit.clause.clause_id for hit in merged}

        # 3.不足 Top-K 时用向量结果补齐：只补未出现过的条款
        for hit in vector_hits:
            if len(merged) >= top_k:
                break
            if hit.clause.clause_id in seen:
                continue
            merged.append(hit)
            seen.add(hit.clause.clause_id)

        return merged[:top_k]

    # 安全调用单路检索
    def _safe(self, retriever, query: PolicyQuery) -> list[PolicyHit]:
        """
        调用单路检索并吞掉异常
            :param retriever: 检索器
            :param query: 查询条件
            :return: 命中列表；异常时为空
        """
        try:
            return retriever.retrieve(query)
        except Exception:
            logger.warning("混合检索单路失败，已忽略 backend=%s", type(retriever).__name__, exc_info=True)
            return []
