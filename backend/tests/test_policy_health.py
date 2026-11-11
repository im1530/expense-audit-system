"""
制度检索自检测试（离线，不联网、不加载向量模型）
    覆盖：
        - 正常初始化：后端、条款数、耗时；
        - 配置 chroma 但实际 BM25：标记降级；
        - 初始化异常：不抛出、记录错误并标记降级；
        - RAG 关闭：跳过自检；
        - 自检脚本退出码。
"""
import unittest
from unittest.mock import patch

import tests  # noqa: F401  先设置测试环境变量
from app.config import settings
from app.rag.health import check_policy_backend
from app.rag.loader import load_policy_document
from app.rag.retriever import BM25PolicyRetriever
from scripts import check_policy_backend as script



class FakeRetriever:
    """最小检索器替身"""

    def __init__(self, document):
        """
        :param document: 制度文档
        """
        self.document = document



class PolicyHealthTest(unittest.TestCase):
    """启动自检"""

    def test_ok(self):
        """正常初始化：返回后端与条款数"""
        document = load_policy_document()
        with patch("app.rag.health.get_retriever", return_value=FakeRetriever(document)):
            result = check_policy_backend()

        self.assertTrue(result["enabled"])
        self.assertEqual(result["backend"], "FakeRetriever")
        self.assertEqual(result["clauses"], len(document.clauses))
        self.assertFalse(result["degraded"])
        self.assertIsNone(result["error"])

    def test_degraded_when_bm25_fallback(self):
        """配置 chroma 但实际回退 BM25：标记降级"""
        document = load_policy_document()
        with patch.object(settings, "POLICY_RETRIEVER", "chroma"), \
                patch("app.rag.health.get_retriever", return_value=BM25PolicyRetriever(document)):
            result = check_policy_backend()

        self.assertEqual(result["backend"], "BM25PolicyRetriever")
        self.assertTrue(result["degraded"])

    def test_failure_does_not_raise(self):
        """初始化失败：不抛异常，记录错误并标记降级"""
        with patch("app.rag.health.get_retriever", side_effect=RuntimeError("no model")):
            result = check_policy_backend()

        self.assertTrue(result["degraded"])
        self.assertIn("no model", result["error"])
        self.assertIsNone(result["backend"])

    def test_disabled_skips(self):
        """RAG 关闭：直接跳过"""
        with patch.object(settings, "POLICY_RAG_ENABLED", False):
            result = check_policy_backend()

        self.assertFalse(result["enabled"])
        self.assertIsNone(result["backend"])
        self.assertFalse(result["degraded"])

    def test_script_exit_codes(self):
        """自检脚本：正常 0，降级 1"""
        with patch.object(script, "check_policy_backend", return_value={
            "enabled": True, "configured": "chroma", "backend": "ChromaPolicyRetriever",
            "clauses": 33, "degraded": False, "error": None, "elapsed_ms": 1,
        }):
            self.assertEqual(script.main(), 0)

        with patch.object(script, "check_policy_backend", return_value={
            "enabled": True, "configured": "chroma", "backend": "BM25PolicyRetriever",
            "clauses": 33, "degraded": True, "error": None, "elapsed_ms": 1,
        }):
            self.assertEqual(script.main(), 1)


if __name__ == "__main__":
    unittest.main()
