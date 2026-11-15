"""
blind_v2 冻结样本的离线校验（不联网、不连库、不调 LLM）

    覆盖：
        1. 哈希清单与文件本体一致；
        2. 条数/编号唯一/分层 24-18-18；
        3. 高危层与 v1 高危清单完全一致；
        4. 无泄题字段（评测标签、场景类别、规则命中、AI 结论）；
        5. 标题无评测变体后缀；
        6. 用同一预登记种子重跑，抽样与映射逐条一致。
"""
import json
import unittest

from evaluation.build_blind_v2 import (
    BANNED_KEYS,
    BANNED_TEXT,
    ITEM_KEYS,
    N_HIGH,
    N_LOW,
    N_MID,
    N_TOTAL,
    OUT_HASH,
    OUT_MAPPING,
    OUT_SAMPLE,
    ROW_KEYS,
    SOURCE_HIGH,
    TITLE_SUFFIXES,
    build_payload,
    sha256_bytes,
    sha256_file,
)
from evaluation.build_blind_v2 import _canonical, _walk_keys


class BlindV2FreezeTest(unittest.TestCase):
    """冻结产物可复现、无泄题、分层正确"""

    @classmethod
    def setUpClass(cls):
        """加载冻结产物，并按原种子重建一份用于比对"""
        cls.payload = json.loads(OUT_SAMPLE.read_text(encoding="utf-8"))  # 冻结内容
        cls.mapping_payload = json.loads(OUT_MAPPING.read_text(encoding="utf-8"))  # 映射
        cls.rows = cls.payload["cases"]  # 60 条事实
        cls.meta = cls.payload["meta"]  # 元数据
        cls.mapping = cls.mapping_payload["mapping"]  # 60 条映射
        # 用冻结时记录的种子与时间重建，验证"谁重跑都是同一批"
        cls.rebuilt, cls.rebuilt_mapping = build_payload(cls.meta["seed"], cls.meta["frozen_at"])

    def test_hash_manifest(self):
        """哈希清单必须与两个文件本体一致"""
        expected = {}  # 文件名 → 哈希
        for line in OUT_HASH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(maxsplit=1)  # 拆分 sha256sum 行
                expected[name.strip()] = digest
        self.assertEqual(expected[OUT_SAMPLE.name], sha256_file(OUT_SAMPLE))
        self.assertEqual(expected[OUT_MAPPING.name], sha256_file(OUT_MAPPING))

    def test_content_hash(self):
        """内容哈希必须与 cases 一致（改 meta 不算改样本）"""
        self.assertEqual(self.meta["content_sha256"], sha256_bytes(_canonical(self.rows)))

    def test_counts_and_unique_ids(self):
        """60 条、编号唯一、格式 B001–B060"""
        self.assertEqual(len(self.rows), N_TOTAL)
        blind_ids = [row["blind_id"] for row in self.rows]  # 盲编号
        self.assertEqual(len(set(blind_ids)), N_TOTAL)
        self.assertTrue(all(blind_id.startswith("B") and len(blind_id) == 4 for blind_id in blind_ids))
        self.assertEqual(sorted(blind_ids)[0], "B001")

    def test_strata(self):
        """分层数量 24/18/18"""
        strata = {}  # 分层计数
        for item in self.mapping:
            strata[item["stratum"]] = strata.get(item["stratum"], 0) + 1
        self.assertEqual(strata, {"high": N_HIGH, "low": N_LOW, "mid": N_MID})

    def test_high_risk_set_matches_v1(self):
        """高危层必须与 v1 高危 24 条完全一致（抽样框未漂移）"""
        v1 = json.loads(SOURCE_HIGH.read_text(encoding="utf-8"))  # v1 高危清单
        v1_ids = {item["case_id"] for item in v1["cases"]}
        high_ids = {item["case_id"] for item in self.mapping if item["stratum"] == "high"}
        self.assertEqual(high_ids, v1_ids)

    def test_no_leak_fields(self):
        """不得出现评测标签、场景类别、规则命中、AI 结论等泄题字段"""
        for row in self.rows:
            for key in _walk_keys(row):
                self.assertNotIn(key, BANNED_KEYS, f"{row['blind_id']} 含泄题字段 {key}")
            text = json.dumps(row, ensure_ascii=False)  # 序列化查文本
            for token in BANNED_TEXT:
                self.assertNotIn(token, text, f"{row['blind_id']} 含泄题文本 {token}")

    def test_field_allowlist(self):
        """只保留白名单字段，防止未来新增字段把标签带进来"""
        for row in self.rows:
            self.assertTrue(set(row) <= set(ROW_KEYS), sorted(set(row) - set(ROW_KEYS)))
            for item in row["items"]:
                self.assertTrue(set(item) <= set(ITEM_KEYS), sorted(set(item) - set(ITEM_KEYS)))

    def test_no_variant_suffix(self):
        """标题与说明里不得残留评测变体后缀"""
        for row in self.rows:
            for suffix in TITLE_SUFFIXES:
                self.assertNotIn(suffix, row["title"])
                for item in row["items"]:
                    self.assertNotIn(suffix, item.get("description") or "")

    def test_mapping_bijection(self):
        """编号与案例一一对应，无重复案例"""
        self.assertEqual(len(self.mapping), N_TOTAL)
        self.assertEqual(len({item["blind_id"] for item in self.mapping}), N_TOTAL)
        self.assertEqual(len({item["case_id"] for item in self.mapping}), N_TOTAL)

    def test_rebuild_is_deterministic(self):
        """同一预登记种子重跑，逐条一致"""
        self.assertEqual(self.rebuilt["cases"], self.rows)
        self.assertEqual(self.rebuilt_mapping["mapping"], self.mapping)


if __name__ == "__main__":
    unittest.main()
