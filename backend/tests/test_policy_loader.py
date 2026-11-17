"""
制度文档解析测试（离线，不联网、不查库）
    覆盖：
        - 真实制度文档可解析、条款编号唯一、正文逐字保留；
        - 文档/条款元数据缺失、版本不一致、编号重复、标签不成对等错误；
        - applies_to 取值校验；
        - 路径解析。
"""
import unittest
from pathlib import Path

import tests  # noqa: F401  先设置测试环境变量
from app.rag.loader import (
    PolicyDocError,
    load_policy_document,
    parse_policy_document,
    resolve_policy_file,
)



# 仓库根目录下当前运行的制度文件（v1.2 保留给 blind_v2 冻结评测）
POLICY_PATH = Path(__file__).resolve().parents[2] / "docs" / "policies" / "expense_policy_v1_3.md"



# 构造一份最小制度文档
def make_document(clause_body: str, *, doc_version: str = "v1.0") -> str:
    """
    拼一份测试用制度文档
        :param clause_body: 条款块内容
        :param doc_version: 文档版本
        :return: Markdown 文本
    """
    return (
        "# 测试制度\n\n"
        "<!-- policy:doc\n"
        "doc_id: test_policy\n"
        f"version: {doc_version}\n"
        "title: 测试制度\n"
        "-->\n\n"
        "## 1 测试章节\n\n"
        f"{clause_body}\n"
    )



# 构造一条合法条款
def make_clause(
    clause_id: str = "1.1",
    *,
    version: str = "v1.0",
    applies_to: str = "types=transportation",
    text: str = "这是一条用于测试的制度条款正文。",
) -> str:
    """
    拼一条测试条款
        :param clause_id: 条款编号
        :param version: 条款版本
        :param applies_to: 适用范围
        :param text: 条款正文
        :return: 条款 Markdown
    """
    return (
        "<!-- policy:clause\n"
        f"clause_id: {clause_id}\n"
        f"version: {version}\n"
        f"applies_to: {applies_to}\n"
        "keywords: 测试,制度\n"
        "-->\n"
        f"{text}\n"
        "<!-- /policy:clause -->"
    )



class PolicyLoaderTest(unittest.TestCase):
    """制度文档解析与校验"""

    def test_real_document_is_valid(self):
        """真实制度文档可解析，33 条条款且编号唯一"""
        document = load_policy_document(POLICY_PATH)
        self.assertEqual(document.doc_id, "expense_policy")
        self.assertEqual(document.version, "v1.3")
        self.assertEqual(len(document.clauses), 33)
        self.assertEqual(len({clause.clause_id for clause in document.clauses}), 33)

        # 每条条款都必须有章节、正文与关键词
        for clause in document.clauses:
            self.assertTrue(clause.section)
            self.assertGreaterEqual(len(clause.text), 10)
            self.assertTrue(clause.keywords)

    def test_text_is_preserved_verbatim(self):
        """条款正文逐字保留，供引用子串校验"""
        document = load_policy_document(POLICY_PATH)
        clause = next(item for item in document.clauses if item.clause_id == "5.4")
        self.assertIn("提交后单据状态置为待审批", clause.text)
        self.assertIn("公司董事会", clause.text)

    def test_duplicate_clause_id_rejected(self):
        """条款编号重复 → 解析失败"""
        text = make_document(make_clause("1.1") + "\n\n" + make_clause("1.1"))
        with self.assertRaises(PolicyDocError):
            parse_policy_document(text)

    def test_missing_doc_metadata_rejected(self):
        """缺少 policy:doc 块 → 解析失败"""
        text = "## 1 测试章节\n\n" + make_clause("1.1")
        with self.assertRaises(PolicyDocError):
            parse_policy_document(text)

    def test_version_mismatch_rejected(self):
        """条款版本与文档版本不一致 → 解析失败"""
        text = make_document(make_clause("1.1", version="v2.0"), doc_version="v1.0")
        with self.assertRaises(PolicyDocError):
            parse_policy_document(text)

    def test_invalid_applies_to_rejected(self):
        """applies_to 使用未知费用大类 → 解析失败"""
        text = make_document(make_clause("1.1", applies_to="types=unknown_type"))
        with self.assertRaises(PolicyDocError):
            parse_policy_document(text)

    def test_unclosed_tag_rejected(self):
        """条款结束标签缺失 → 解析失败"""
        block = make_clause("1.1").replace("<!-- /policy:clause -->", "")
        with self.assertRaises(PolicyDocError):
            parse_policy_document(make_document(block))

    def test_unknown_metadata_key_rejected(self):
        """条款元数据出现未知键 → 解析失败"""
        block = make_clause("1.1").replace("keywords: 测试,制度", "keywords: 测试,制度\nunknown: x")
        with self.assertRaises(PolicyDocError):
            parse_policy_document(make_document(block))

    def test_default_policy_path_exists(self):
        """默认配置解析出的制度文件存在"""
        self.assertTrue(resolve_policy_file().is_file())


if __name__ == "__main__":
    unittest.main()
