"""
Chroma 向量检索实现（Phase D 可选后端）
    设计目标：
        - 与 BM25 实现同一 PolicyRetriever 协议，业务层（policy_service / risk_agent）零改动；
        - 条款文本向量化后按余弦相似度检索，解决"口语化表述字面不重叠"的漏召回；
        - 条款元数据变化（制度升版本、条款增删）时自动重建集合。
    关键取舍：
        - 先向量召回 N 条，再用 clause_applies 做 applies_to 硬过滤（Chroma 的 where 无法表达列表交集）；
        - 相似度阈值单独配置（POLICY_CHROMA_MIN_SIMILARITY），不复用 BM25 的绝对分；
        - 持久化目录可配置；测试可注入内存 client 与假 embedding 函数，不触发模型下载。
    边界：
        - 只做检索，不调用 LLM、不写业务库；
        - 集合重建失败时抛异常，由工厂回退 BM25。
"""
import logging
from pathlib import Path

from app.config import settings
from app.rag.embedding import build_embedding_function
from app.rag.filters import clause_applies
from app.rag.loader import BACKEND_DIR
from app.rag.schema import PolicyClause, PolicyDocument, PolicyHit, PolicyQuery


logger = logging.getLogger(__name__)

# 向量召回的候选倍数：先多取一些，过滤完再截断 Top-K
CANDIDATE_FACTOR = 4

# 向量文档的拼装版本：参与 embedding 的文本口径变化时必须改这里，
# 否则旧集合不会自动重建，会导致"改了代码但检索结果没变"的假象。
EMBED_DOC_VERSION = "v2_kw_first"



# 组装参与 embedding 的条款文本
def build_embed_text(clause: PolicyClause) -> str:
    """
    组装一条条款用于向量化的文本
        口径：**关键词在前、原文在后**。
        原因（2026-11-11 离线实测，68 条有核心条款的评测案例）：
            - 只向量化原文：核心条款检索覆盖 82.4%（重复报销类因 1.2 排第 16 被 top-4 截断而漏检）；
            - 原文在前、关键词在后：94.1%；
            - 关键词在前、原文在后：**100%**，且"相关条款占比"由 32.6% 提升到 36.5%，没有变差。
        长度：最长条款拼装后 172 字符，远小于 POLICY_EMBEDDING_MAX_LENGTH(256)，不会被截断。
        :param clause: 制度条款
        :return: 用于 embedding 的文本
    """
    keywords = " ".join(clause.keywords)                           # 人工维护的检索关键词
    return f"{keywords}\n{clause.text}"                            # 关键词在前 + 原文



# 解析持久化目录
def resolve_chroma_dir(persist_dir: str | None = None) -> Path:
    """
    把配置里的目录解析成绝对路径（相对路径按 backend 目录解析）
        :param persist_dir: 覆盖目录；为空读配置
        :return: 绝对路径
    """
    value = persist_dir if persist_dir is not None else settings.POLICY_CHROMA_DIR
    path = Path(value)
    return path if path.is_absolute() else BACKEND_DIR / path



# Chroma 检索器
class ChromaPolicyRetriever:
    """
    基于 Chroma + Embedding 的制度检索器
        :ivar document: 当前制度文档
        :ivar _collection: Chroma 集合
        :ivar _by_id: 条款编号索引
    """

    def __init__(
        self,
        document: PolicyDocument,
        *,
        client=None,
        embedding_function=None,
        persist_dir: str | None = None,
        collection_name: str | None = None,
    ):
        """
        :param document: 制度文档
        :param client: 可注入的 Chroma client（测试用内存实例）
        :param embedding_function: 可注入的 embedding 函数（测试用假函数）
        :param persist_dir: 持久化目录；为空读配置
        :param collection_name: 集合名；为空读配置
        """
        # 1.延迟导入 chromadb：未安装时不影响 BM25 主流程
        import chromadb

        # 2.保存基础依赖
        self.document = document
        self._by_id = {clause.clause_id: clause for clause in document.clauses}
        self._embedding_function = embedding_function or build_embedding_function()
        self._collection_name = collection_name or settings.POLICY_CHROMA_COLLECTION

        # 3.创建 client：注入优先，其次持久化目录
        if client is not None:
            self._client = client
        else:
            self._client = chromadb.PersistentClient(path=str(resolve_chroma_dir(persist_dir)))

        # 4.建集合并写入条款
        self._collection = self._ensure_collection()

    # 集合名（测试/排障用）
    @property
    def collection_name(self) -> str:
        """
        当前集合名
            :return: 集合名
        """
        return self._collection_name

    # embedding 模型签名
    def _embedding_signature(self) -> str:
        """
        生成集合签名（目录 + 池化 + 长度 + 向量文本口径），用于判断是否需要重建集合
            :return: 签名字符串
        """
        try:
            config = self._embedding_function.get_config()
            return (
                f"{config.get('model_dir')}|{config.get('pooling')}|{config.get('max_length')}"
                f"|doc={EMBED_DOC_VERSION}"                          # 文本口径变化也必须触发重建
            )
        except Exception:
            return "unknown"

    # 确保集合与制度版本一致
    def _ensure_collection(self):
        """
        获取或重建集合：条款数量或制度版本变化时整库重建
            :return: Chroma collection
        """
        # 1.尝试复用已有集合
        try:
            collection = self._client.get_collection(
                self._collection_name,
                embedding_function=self._embedding_function,
            )
            metadata = collection.metadata or {}
            same_version = metadata.get("policy_version") == self.document.version
            same_size = collection.count() == len(self.document.clauses)
            same_model = metadata.get("embedding_model") == self._embedding_signature()
            if same_version and same_size and same_model:
                return collection
            logger.info(
                "制度向量集合需要重建 version=%s->%s size=%s->%s model_same=%s",
                metadata.get("policy_version"), self.document.version,
                collection.count(), len(self.document.clauses), same_model,
            )
        except Exception:
            # 集合不存在或读取失败：走重建
            pass

        # 2.删除旧集合并重建（cosine 距离，便于转成相似度）
        try:
            self._client.delete_collection(self._collection_name)
        except Exception:
            pass
        collection = self._client.create_collection(
            name=self._collection_name,
            embedding_function=self._embedding_function,
            metadata={
                "policy_version": self.document.version,
                "embedding_model": self._embedding_signature(),
                "hnsw:space": "cosine",
            },
        )

        # 3.写入全部条款：文档 = 关键词 + 条款原文（见 build_embed_text），元数据只放章节
        collection.add(
            ids=[clause.clause_id for clause in self.document.clauses],
            documents=[build_embed_text(clause) for clause in self.document.clauses],
            metadatas=[{"section": clause.section} for clause in self.document.clauses],
        )
        logger.info("制度向量集合构建完成 clauses=%s version=%s", len(self.document.clauses), self.document.version)
        return collection

    # 执行检索
    def retrieve(self, query: PolicyQuery) -> list[PolicyHit]:
        """
        先向量召回候选条款，再做 applies_to 过滤与相似度阈值
            :param query: 查询条件
            :return: 命中列表（相似度降序、编号升序）
        """
        # 1.空查询直接返回
        text = str(query.text or "").strip()
        if not text or not self._by_id:
            return []

        # 2.向量召回候选：多取一些，给硬过滤留余量
        top_k = max(1, int(settings.POLICY_TOP_K))
        n_results = min(
            max(top_k * CANDIDATE_FACTOR, 20),
            max(1, len(self.document.clauses)),
        )
        result = self._collection.query(query_texts=[text], n_results=n_results)
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        # 3.距离转相似度，过滤不适用的条款，再按阈值截断
        hits: list[PolicyHit] = []
        for clause_id, distance in zip(ids, distances):
            clause = self._by_id.get(clause_id)
            if clause is None or not clause_applies(clause, query):
                continue
            similarity = 1.0 - float(distance)
            if similarity < float(settings.POLICY_CHROMA_MIN_SIMILARITY):
                continue
            hits.append(PolicyHit(clause=clause, score=round(similarity, 4)))

        # 4.排序：相似度降序，同分按条款编号升序，保证可复现
        hits.sort(key=lambda hit: (-hit.score, hit.clause.clause_id))
        return hits[:top_k]
