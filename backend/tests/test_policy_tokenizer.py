"""
制度文本处理测试（离线，不联网）
    覆盖：全半角归一化、中文 bigram、英文/数字切分、同义词扩展、
          引用比对归一化、外层引号剥离。
"""
import unittest

import tests  # noqa: F401  先设置测试环境变量
from app.rag.tokenizer import (
    expand_tokens,
    normalize_for_match,
    normalize_text,
    strip_wrapping_quotes,
    tokenize,
)



class TokenizerTest(unittest.TestCase):
    """文本处理"""

    def test_normalize_fullwidth_and_case(self):
        """全角字符与大小写统一"""
        self.assertEqual(normalize_text("ＡＢＣ１２３"), "abc123")

    def test_chinese_bigram(self):
        """中文按 bigram 切分"""
        tokens = tokenize("高铁票")
        self.assertIn("高铁", tokens)
        self.assertIn("铁票", tokens)

    def test_english_and_number(self):
        """英文/数字整段保留，金额额外保留整数部分"""
        tokens = tokenize("EXP2026 金额 2000.50 元")
        self.assertIn("exp2026", tokens)
        self.assertIn("2000.50", tokens)
        self.assertIn("2000", tokens)

    def test_synonym_expansion(self):
        """打车可扩展到出租车/网约车"""
        tokens = expand_tokens(tokenize("打车费"))
        self.assertIn("出租车", tokens)
        self.assertIn("网约车", tokens)

    def test_normalize_for_match_removes_whitespace(self):
        """引用比对忽略空白"""
        self.assertEqual(
            normalize_for_match("员工 出差\n应优先"),
            normalize_for_match("员工出差应优先"),
        )

    def test_strip_wrapping_quotes(self):
        """去掉模型自动加的外层引号"""
        self.assertEqual(strip_wrapping_quotes("“员工出差应优先”"), "员工出差应优先")
        self.assertEqual(strip_wrapping_quotes("\"员工出差\""), "员工出差")

    def test_no_tokens_for_blank(self):
        """空白文本没有词项"""
        self.assertEqual(tokenize("   "), [])


if __name__ == "__main__":
    unittest.main()
