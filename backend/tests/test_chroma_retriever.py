"""
Chroma 向量检索与混合检索测试（离线，不联网、不下载模型）
    覆盖：
        - 向量检索：条款入库、Top-K 截断、applies_to 过滤、空查询、相似度阈值；
        - 制度版本/条款数量变化时集合重建；
        - 混合检索：关键词结果优先、向量补位、去重、Top-K；
        - 工厂降级：Chroma 初始化失败回退 BM25。
    说明：用假 embedding 函数 + 内存 client，不触发 ONNX 模型加载。
"""
import hashlib
import unittest
from decimal import Decimal
from unittest.mock import patch

import numpy as np

import tests  # noqa: F401  先设置测试环境变量
from app.config import settings
from app.rag import chroma_retriever as chroma_retriever_module
from app.rag.chroma_retriever import EMBED_DOC_VERSION, ChromaPolicyRetriever, build_embed_text
from app.rag.hybrid_retriever import HybridPolicyRetriever
from app.rag.loader import load_policy_document
from app.rag.retriever import BM25PolicyRetriever, build_retriever
from app.rag.schema import PolicyDocument, PolicyHit, PolicyQuery


# 测试用向量维度
DIM = 8



# 假 embedding 函数：按字符哈希生成稳定向量
class FakeEmbeddingFunction:
    """不依赖模型文件的确定性 embedding"""

    @staticmethod
    def name() -> str:
        """函数名（Chroma 配置用）"""
        return "fake"

    def default_space(self) -> str:
        """默认距离空间"""
        return "cosine"

    def supported_spaces(self) -> list[str]:
        """支持的距离空间"""
        return ["cosine", "l2", "ip"]

    def get_config(self) -> dict:
        """可序列化配置"""
        return {"model_dir": "fake", "pooling": "mean", "max_length": 8}

    @classmethod
    def build_from_config(cls, config: dict) -> "FakeEmbeddingFunction":
        """从配置重建"""
        return cls()

    def embed_documents(self, input: list[str]) -> list[np.ndarray]:
        """文档向量化"""
        return self(input)

    def embed_query(self, input: list[str]) -> list[np.ndarray]:
        """查询向量化"""
        return self(input)

    def __call__(self, input: list[str]) -> list[np.ndarray]:
        """生成稳定向量"""
        vectors = []
        for text in input:
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            raw = np.array([digest[index] for index in range(DIM)], dtype=np.float32) + 1.0
            vectors.append((raw / np.linalg.norm(raw)).astype(np.float32))
        return vectors



# 构造查询
def make_query(text: str, *, types=(), role: str = "") -> PolicyQuery:
    """
    构造检索查询
        :param text: 查询文本
        :param types: 费用大类
        :param role: 角色
        :return: PolicyQuery
    """
    return PolicyQuery(
        text=text,
        expense_types=frozenset(types),
        role=role,
        total_amount=Decimal("100"),
    )



class ChromaRetrieverTest(unittest.TestCase):
    """向量检索"""

    @classmethod
    def setUpClass(cls):
        """加载真实制度文档"""
        cls.document = load_policy_document()

    def make_retriever(self, document=None, *, client=None):
        """
        构造内存版检索器
            :param document: 制度文档
            :param client: 复用同一个内存 client（重建测试用）
            :return: ChromaPolicyRetriever
        """
        import chromadb

        return ChromaPolicyRetriever(
            document or self.document,
            client=client or chromadb.EphemeralClient(),
            embedding_function=FakeEmbeddingFunction(),
            collection_name="test_policy",
        )

    def test_retrieve_returns_applicable_hits(self):
        """返回 Top-K 内、按分数降序、且符合 applies_to 的条款"""
        retriever = self.make_retriever()
        hits = retriever.retrieve(make_query("酒店住宿费", types=("accommodation",)))

        self.assertGreater(len(hits), 0)
        self.assertLessEqual(len(hits), settings.POLICY_TOP_K)
        scores = [hit.score for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for hit in hits:
            self.assertTrue(
                not hit.clause.expense_types or "accommodation" in hit.clause.expense_types
            )

    def test_empty_query_returns_empty(self):
        """空查询直接返回空"""
        retriever = self.make_retriever()
        self.assertEqual(retriever.retrieve(make_query("   ")), [])

    def test_min_similarity_filters_all(self):
        """相似度阈值抬高后返回空"""
        retriever = self.make_retriever()
        with patch.object(settings, "POLICY_CHROMA_MIN_SIMILARITY", 2.0):
            self.assertEqual(retriever.retrieve(make_query("酒店住宿费")), [])

    def test_rebuild_when_clause_count_changes(self):
        """条款数量变化：同一 client 下集合重建为新的条款集"""
        import chromadb

        client = chromadb.EphemeralClient()
        self.make_retriever(client=client)
        smaller = PolicyDocument(
            doc_id=self.document.doc_id,
            title=self.document.title,
            version=self.document.version,
            clauses=self.document.clauses[:5],
        )
        rebuilt = self.make_retriever(smaller, client=client)
        allowed = {clause.clause_id for clause in smaller.clauses}
        hits = rebuilt.retrieve(make_query("报销"))
        self.assertTrue(all(hit.clause.clause_id in allowed for hit in hits))

    def test_embed_text_puts_keywords_first(self):
        """向量文本 = 关键词在前 + 原文在后"""
        clause = self.document.clauses[0]
        text = build_embed_text(clause)
        first_line = text.split("\n")[0]                       # 第一行应是关键词
        self.assertEqual(first_line, " ".join(clause.keywords))
        self.assertTrue(text.endswith(clause.text))            # 原文完整保留在末尾
        for keyword in clause.keywords:                        # 每个关键词都参与 embedding
            self.assertIn(keyword, text)

    def test_signature_includes_doc_version(self):
        """签名包含向量文本口径，口径变化才能触发重建"""
        retriever = self.make_retriever()
        self.assertIn(f"doc={EMBED_DOC_VERSION}", retriever._embedding_signature())

    def test_collection_documents_contain_keywords(self):
        """入库文档包含关键词（保证"关键词方案"对 chroma 真正生效）"""
        retriever = self.make_retriever()
        data = retriever._collection.get(ids=["1.2"], include=["documents"])
        stored = data["documents"][0]
        clause = retriever._by_id["1.2"]
        self.assertIn("重复报销", stored)                       # 1.2 的人工关键词
        self.assertIn(clause.text, stored)                      # 原文仍在

    def test_rebuild_when_doc_version_changes(self):
        """文本口径变化：同一 client 下旧集合被重建（避免"改了代码检索不变"）"""
        import chromadb

        client = chromadb.EphemeralClient()
        self.make_retriever(client=client)
        with patch.object(chroma_retriever_module, "EMBED_DOC_VERSION", "v9_test"):
            rebuilt = self.make_retriever(client=client)
        signature = rebuilt._collection.metadata.get("embedding_model", "")
        self.assertIn("doc=v9_test", signature)



class HybridRetrieverTest(unittest.TestCase):
    """混合检索"""

    @classmethod
    def setUpClass(cls):
        """准备条款"""
        cls.document = load_policy_document()
        cls.clauses = {clause.clause_id: clause for clause in cls.document.clauses}

    def hit(self, clause_id: str, score: float = 1.0) -> PolicyHit:
        """
        构造命中
            :param clause_id: 条款编号
            :param score: 分数
            :return: PolicyHit
        """
        return PolicyHit(clause=self.clauses[clause_id], score=score)

    def test_keyword_first_then_vector(self):
        """关键词结果优先，向量补齐不足部分"""
        class FakeVector:
            """固定返回给定的向量结果"""

            def __init__(self, hits):
                self.hits = hits

            def retrieve(self, query):
                return list(self.hits)

        retriever = HybridPolicyRetriever(
            self.document,
            keyword=BM25PolicyRetriever(self.document),
            vector=FakeVector([self.hit("3.1"), self.hit("3.2")]),
        )
        hits = retriever.retrieve(make_query("高铁票", types=("transportation",)))
        ids = [hit.clause.clause_id for hit in hits]
        self.assertEqual(ids[0], "3.1")                 # 关键词 Top-1 保留在最前
        self.assertLessEqual(len(ids), settings.POLICY_TOP_K)
        self.assertEqual(len(ids), len(set(ids)))       # 不重复

    def test_vector_fills_when_keyword_empty(self):
        """关键词为空时完全由向量补位"""
        class EmptyKeyword:
            """关键词无命中"""

            def retrieve(self, query):
                return []

        class FakeVector:
            """固定返回 2.2"""

            def __init__(self):
                self.hits = [self_hit]

            def retrieve(self, query):
                return list(self.hits)

        self_hit = self.hit("2.2")
        retriever = HybridPolicyRetriever(self.document, keyword=EmptyKeyword(), vector=FakeVector())
        hits = retriever.retrieve(make_query("发票"))
        self.assertEqual([hit.clause.clause_id for hit in hits], ["2.2"])

    def test_single_side_failure_is_tolerated(self):
        """单路异常不影响另一路"""
        class Broken:
            """总是抛错"""

            def retrieve(self, query):
                raise RuntimeError("boom")

        class FakeVector:
            """固定返回 2.2"""

            def __init__(self):
                self.hits = [self_hit]

            def retrieve(self, query):
                return list(self.hits)

        self_hit = self.hit("2.2")
        retriever = HybridPolicyRetriever(self.document, keyword=Broken(), vector=FakeVector())
        hits = retriever.retrieve(make_query("发票"))
        self.assertEqual([hit.clause.clause_id for hit in hits], ["2.2"])



class RetrieverFactoryTest(unittest.TestCase):
    """工厂与降级"""

    def test_chroma_failure_falls_back_to_bm25(self):
        """Chroma 初始化失败：回退 BM25"""
        document = load_policy_document()
        with patch.object(settings, "POLICY_RETRIEVER", "chroma"), \
                patch("app.rag.chroma_retriever.ChromaPolicyRetriever", side_effect=RuntimeError("no model")):
            retriever = build_retriever(document)
        self.assertIsInstance(retriever, BM25PolicyRetriever)

    def test_default_backend_is_chroma(self):
        """配置默认使用 Chroma 向量检索（单测环境会通过环境变量覆盖为 bm25）"""
        from app.config import Settings

        self.assertEqual(Settings.model_fields["POLICY_RETRIEVER"].default, "chroma")



if __name__ == "__main__":
    unittest.main()
