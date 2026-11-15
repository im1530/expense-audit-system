"""
冻结 blind_v2 独立双标样本（P0-1 第①步：抽 60 条并冻结）

    做什么：
        1. 以 golden_v1_real96.json 为抽样框：高危 24 条全取 + low 18 + mid 18 = 60 条；
        2. 固定预登记种子抽样，任何人重跑都得到同一批 60 条；
        3. 只保留单据事实，剥掉全部评测标签、AI 痕迹与变体后缀（标题后缀、场景类别是两个泄题点）；
        4. 输出 blind_v2.json（60 条事实）+ blind_v2_mapping.json（B 编号 ↔ G 编号 + 分层）
           + blind_v2.sha256（两份文件的哈希），冻结后设只读；
        5. --verify 重算哈希、重跑抽样、扫描泄题字段，任何一项不符即退出码 1。

    用法（在 backend/ 下）：
        python evaluation/build_blind_v2.py            # 生成并冻结
        python evaluation/build_blind_v2.py --verify   # 只校验，不写盘
        python evaluation/build_blind_v2.py --force    # 解冻重写（需先升版本，仅限开发期）

    说明：
        - 不连数据库、不调 LLM、不读 .env，纯离线；
        - 盲包（xlsx）由第②步生成，本脚本只负责"冻结哪 60 条、每条给什么事实"；
        - 本文件是内部分析件，不发给标注人；发给标注人的是第②步的盲包。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import stat
from datetime import datetime
from pathlib import Path
from typing import Any


# 本脚本所在目录（backend/evaluation），所有相对路径都以它为准，避免受调用时 CWD 影响
BASE = Path(__file__).resolve().parent
# 案例目录：冻结产物与来源都在这里
CASES_DIR = BASE / "cases"
# 抽样框：96 条冻结真实集（已剔除 4 条 ai_fallback）
SOURCE_REAL = CASES_DIR / "golden_v1_real96.json"
# 高危 24 条（v1，AI 预填 + 人工确认；这里只用作抽样框，不作为标签证据）
SOURCE_HIGH = CASES_DIR / "high_risk_final_v1.json"
# 制度原文：标注指南里要附 33 条条款，这里记录其哈希，防止"标注中途改制度"
SOURCE_POLICY = BASE.parents[1] / "docs" / "policies" / "expense_policy_v1.md"

# 冻结产物：60 条事实（不含 G 编号与分层）
OUT_SAMPLE = CASES_DIR / "blind_v2.json"
# 盲编号 ↔ 案例编号 ↔ 分层，只给统计与裁决用，不进盲包
OUT_MAPPING = CASES_DIR / "blind_v2_mapping.json"
# 两份产物的 sha256（sha256sum 格式）
OUT_HASH = CASES_DIR / "blind_v2.sha256"

# 预登记种子：生成前就写进方案，避免"看完结果再挑种子"
DEFAULT_SEED = "blind_v2_2026-11-14"
# 种子登记日期（审计用）
SEED_REGISTERED_AT = "2026-11-14"
# 标注指南版本：v2.0 定稿后其 sha256 记入发放清单（第②步）
GUIDE_VERSION = "annotate_guide_v2.0"

# 高危全取
N_HIGH = 24
# 低风险抽 18（池子 24 条）
N_LOW = 18
# 中风险抽 18（池子 48 条）
N_MID = 18
# 冻结条数
N_TOTAL = N_HIGH + N_LOW + N_MID

# 标题里四个已知的评测变体后缀，全部剥掉（各 24 条）
TITLE_SUFFIXES = ("标准场景", "金额上浮20%", "金额下浮20%", "补充说明场景")

# 费用类型 → 中文标签；expense_type 才是真实费用类别，item 的 category 是评测场景名
EXPENSE_TYPE_LABELS = {
    "transportation": "交通",
    "meal": "餐饮",
    "accommodation": "住宿",
    "office": "办公",
    "other": "其他",
}

# 冻结条目的字段白名单：只准出现这些键，白名单式投影比黑名单更抗未来字段膨胀
ROW_KEYS = (
    "blind_id", "title", "department", "submitter_role",
    "total_amount", "submitted_at", "items",
)
# 明细字段白名单
ITEM_KEYS = (
    "expense_type", "expense_type_label", "amount", "expense_date",
    "has_invoice", "description", "no_invoice_reason",
)

# 泄题字段黑名单（递归检查键名）：出现任一即判定冻结失败
BANNED_KEYS = frozenset({
    "expected", "simulated", "category", "rule_hits", "document_findings",
    "must_findings", "must_not", "label_status", "note", "review_note",
    "review_status", "annotator", "reviewed_at", "reviewed_by", "eval_scope",
    "frozen", "source", "decision", "risk_level", "score", "findings",
    "case_id", "stratum",
})
# 泄题文本黑名单：标题变体后缀与 AI 痕迹，出现即判定冻结失败
BANNED_TEXT = ("标准场景", "金额上浮", "金额下浮", "补充说明场景", "ai_prefill", "AI 预填")


def _canonical(value: Any) -> bytes:
    """
    把对象序列化成稳定字节（键排序、无多余空白），用于计算内容哈希
        :param value: 任意可 JSON 序列化的对象
        :return: UTF-8 编码的规范字节
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """
    计算字节串的 sha256
        :param data: 待计算字节
        :return: 十六进制摘要
    """
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """
    计算文件的 sha256
        :param path: 文件路径
        :return: 十六进制摘要
    """
    return sha256_bytes(path.read_bytes())


def load_sources() -> tuple[list[dict], set[str]]:
    """
    读取抽样框并校验其结构未变（结构一变，冻结的就不是原来那 60 条了）
        :return: (96 条冻结案例, 高危 24 条 ID 集合)
    """
    cases = json.loads(SOURCE_REAL.read_text(encoding="utf-8"))  # 96 条案例
    if not isinstance(cases, list) or len(cases) != 96:
        raise SystemExit(f"抽样框应为 96 条列表，实际 {type(cases).__name__}/{len(cases) if isinstance(cases, list) else '-'}")

    # 每条都必须是已复核、已冻结的标签，否则不能拿来当盲标样本
    not_frozen = [c["case_id"] for c in cases if not c.get("frozen") or c.get("label_status") != "reviewed"]
    if not_frozen:
        raise SystemExit(f"存在未冻结/未复核案例：{not_frozen[:5]}")

    # v1 高危清单：只用于保证高危样本量，不作为标签证据
    high_payload = json.loads(SOURCE_HIGH.read_text(encoding="utf-8"))
    high_ids = {item["case_id"] for item in high_payload["cases"]}  # 24 个 G 编号
    if len(high_ids) != N_HIGH:
        raise SystemExit(f"v1 高危清单应为 24 条，实际 {len(high_ids)}")

    # 抽样框里 risk_level=high 的集合，必须与 v1 清单完全一致，否则说明两边口径已漂移
    expected_high = {c["case_id"] for c in cases if c["expected"]["risk_level"] == "high"}
    if expected_high != high_ids:
        raise SystemExit(f"高危集合不一致：仅 v1 有 {sorted(high_ids - expected_high)}，仅 real96 有 {sorted(expected_high - high_ids)}")

    return cases, high_ids


def pick_strata(cases: list[dict], high_ids: set[str], seed: str) -> dict[str, list[dict]]:
    """
    分层抽样：高危全取，low/mid 各抽 18 条（候选先排序再洗牌，保证可复现）
        :param cases: 96 条案例
        :param high_ids: 高危 24 条 ID
        :param seed: 预登记种子
        :return: {"high": [...], "low": [...], "mid": [...]}，组内按 case_id 升序
    """
    by_id = {case["case_id"]: case for case in cases}  # G 编号 → 案例
    # 分层抽样专用随机源：与 B 编号的随机源隔开，改编号规则不会影响抽样结果
    rng = random.Random(f"{seed}:strata")

    picked: dict[str, list[dict]] = {}  # 每层抽中的案例
    # high 层全取（已按 case_id 排序，保证输出稳定）
    picked["high"] = [by_id[case_id] for case_id in sorted(high_ids)]

    # low / mid 各自从池子里洗牌后取前 N 条
    for stratum, need in (("low", N_LOW), ("mid", N_MID)):
        pool = sorted(
            case["case_id"]
            for case in cases
            if case["expected"]["risk_level"] == stratum
        )  # 同层候选，先排序
        if len(pool) < need:
            raise SystemExit(f"{stratum} 层只有 {len(pool)} 条，不够抽 {need} 条")
        rng.shuffle(pool)  # 洗牌
        picked[stratum] = [by_id[case_id] for case_id in sorted(pool[:need])]  # 取前 need 条并排序

    return picked


def normalize_title(title: str) -> str:
    """
    剥掉标题末尾的评测变体后缀（如"（标准场景）"）
        :param title: 原始标题
        :return: 只剩业务描述的标题
    """
    for suffix in TITLE_SUFFIXES:  # 四个已知后缀
        marker = f"（{suffix}）"  # 全角括号包裹
        if title.endswith(marker):
            return title[: -len(marker)]  # 去掉后缀
    raise SystemExit(f"标题缺少预期的变体后缀，样本结构已变：{title!r}")


def item_facts(item: dict) -> dict:
    """
    抽取单条明细的事实字段（白名单）
        :param item: context.items 里的一条明细
        :return: 只含事实的字典
    """
    expense_type = item["expense_type"]  # transportation / meal / ...
    if expense_type not in EXPENSE_TYPE_LABELS:
        raise SystemExit(f"未知费用类型：{expense_type!r}")

    facts = {
        "expense_type": expense_type,  # 原始费用类型
        "expense_type_label": EXPENSE_TYPE_LABELS[expense_type],  # 中文费用类别
        "amount": item["amount"],  # 明细金额（字符串，保留两位小数）
        "expense_date": item["expense_date"],  # 费用发生日期
        "has_invoice": bool(item["has_invoice"]),  # 是否有发票/有效凭证
        "description": item.get("description") or "",  # 费用说明（原始事实）
    }
    if item.get("no_invoice_reason"):  # 只有确实填了才带上，避免空字段噪声
        facts["no_invoice_reason"] = item["no_invoice_reason"]
    return facts


def case_facts(case: dict) -> dict:
    """
    抽取单个案例的事实字段（白名单），不含任何标签、规则命中与 AI 结论
        :param case: 96 条案例中的一条
        :return: 只含事实的字典（不含 blind_id）
    """
    context = case["context"]  # 案例上下文
    expense = context["expense"]  # 主单
    items = context["items"]  # 明细列表

    return {
        "title": normalize_title(case["title"]),  # 已剥后缀的标题
        "department": expense.get("department"),  # 报销部门
        "submitter_role": expense.get("submitter_role"),  # 报销人角色（决定审批路径）
        "total_amount": expense.get("total_amount"),  # 单据总金额
        "submitted_at": expense.get("submitted_at"),  # 提交时间（跨期判断要用）
        "items": [item_facts(item) for item in items],  # 明细事实
    }


def build_rows(stratified: dict[str, list[dict]], seed: str) -> tuple[list[dict], list[dict]]:
    """
    跨层混排后编 B001–B060，并生成映射（编号顺序不暴露分层，防止"B001–B024 一定是高危"）
        :param stratified: 分层抽样结果
        :param seed: 预登记种子
        :return: (60 条事实行, 60 条映射)
    """
    # 先把三层拼成一个列表，元素为 (案例, 分层)
    ordered: list[tuple[dict, str]] = []
    for stratum in ("high", "low", "mid"):  # 固定层序，先拼后洗
        ordered.extend((case, stratum) for case in stratified[stratum])

    # B 编号专用随机源：与抽样随机源隔离
    rng = random.Random(f"{seed}:idmap")
    rng.shuffle(ordered)  # 跨层混排

    rows: list[dict] = []  # 60 条事实
    mapping: list[dict] = []  # 60 条映射
    for index, (case, stratum) in enumerate(ordered, start=1):  # 1..60
        blind_id = f"B{index:03d}"  # 盲编号
        rows.append({"blind_id": blind_id, **case_facts(case)})  # blind_id 放最前，便于人工核
        mapping.append({
            "blind_id": blind_id,  # 盲编号
            "case_id": case["case_id"],  # 原案例编号（仅内部可见）
            "stratum": stratum,  # 分层（仅内部可见）
        })

    return rows, mapping


def build_payload(seed: str, frozen_at: str | None = None) -> tuple[dict, dict]:
    """
    组装冻结产物（案例事实 + 元数据）与映射
        :param seed: 预登记种子
        :param frozen_at: 冻结时间；校验时传原值，生成时留空取当前时间
        :return: (blind_v2.json 内容, blind_v2_mapping.json 内容)
    """
    cases, high_ids = load_sources()  # 读取并校验抽样框
    stratified = pick_strata(cases, high_ids, seed)  # 分层抽样
    rows, mapping = build_rows(stratified, seed)  # 编号与映射

    # 冻结时间：生成时取当前时区时间，校验时必须沿用原值，否则内容哈希对不上
    frozen_at = frozen_at or datetime.now().astimezone().isoformat(timespec="seconds")

    # 三份来源的哈希：来源一改，冻结就失效，verify 会报出来
    sources = [
        {"file": "cases/golden_v1_real96.json", "n": len(cases), "sha256": sha256_file(SOURCE_REAL)},
        {"file": "cases/high_risk_final_v1.json", "n": len(high_ids), "sha256": sha256_file(SOURCE_HIGH)},
        {"file": "docs/policies/expense_policy_v1.md", "n": 33, "sha256": sha256_file(SOURCE_POLICY)},
    ]

    payload = {
        "meta": {
            "version": "blind_v2",  # 冻结版本
            "status": "frozen",  # 状态：生成后不再改
            "frozen_at": frozen_at,  # 冻结时间
            "seed": seed,  # 预登记种子
            "seed_registered_at": SEED_REGISTERED_AT,  # 种子登记日期
            "guide_version": GUIDE_VERSION,  # 标注指南版本（定稿后哈希进发放清单）
            "n": len(rows),  # 60
            "strata": {"high": N_HIGH, "low": N_LOW, "mid": N_MID},  # 分层数量
            "sampling": "分层固定种子；high 全取，low/mid 候选按 case_id 排序后洗牌取前 18",  # 抽样方法
            "id_assignment": "三层混排后依次编号 B001–B060，编号顺序不暴露分层",  # 编号方法
            "fields_kept": list(ROW_KEYS),  # 保留字段
            "fields_dropped": ["expected", "simulated", "category", "rule_hits", "document_findings", "case_id", "stratum"],  # 剥离字段
            "sources": sources,  # 来源与哈希
            "content_sha256": sha256_bytes(_canonical(rows)),  # 案例内容哈希（与 meta 解耦）
            "generator": "evaluation/build_blind_v2.py",  # 生成脚本
            "notes": [
                "分层过采样：高危占 24/60，仅用于保证高危样本量，不能外推到总体高危比例。",
                "高危 24 条来自 v1（AI 预填 + 人工确认）的抽样框，只作抽样依据，不作标签证据。",
                "标题评测变体后缀与场景类别已剥离，避免标注人从字段直接读出答案。",
                "样本冻结后再改只能升版本为 blind_v3。",
            ],
        },
        "cases": rows,  # 60 条事实
    }

    mapping_payload = {
        "meta": {
            "version": "blind_v2",  # 与冻结样本同版本
            "n": len(mapping),  # 60
            "note": "盲编号↔案例编号↔分层，仅用于统计与裁决，不得进入盲包。",  # 用途说明
        },
        "mapping": mapping,  # 60 条映射
    }

    return payload, mapping_payload


def _walk_keys(value: Any):
    """
    递归遍历所有字典键
        :param value: 任意 JSON 结构
        :return: 键名迭代器
    """
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def scan_leaks(rows: list[dict]) -> list[str]:
    """
    扫描泄题：禁用键、禁用文本、标题后缀
        :param rows: 60 条事实
        :return: 问题列表（空表示通过）
    """
    problems: list[str] = []  # 收集问题
    for row in rows:  # 逐条
        blind_id = row.get("blind_id", "?")  # 出错时便于定位
        for key in _walk_keys(row):  # 递归查键名
            if key in BANNED_KEYS:
                problems.append(f"{blind_id} 含泄题字段 {key}")
        text = json.dumps(row, ensure_ascii=False)  # 序列化后查文本
        for token in BANNED_TEXT:  # 查禁用文本
            if token in text:
                problems.append(f"{blind_id} 含泄题文本 {token}")
        for item in row.get("items", []):  # 明细里的后缀
            for suffix in TITLE_SUFFIXES:
                if suffix in (item.get("description") or ""):
                    problems.append(f"{blind_id} 明细说明含变体后缀 {suffix}")
    return problems


def write_outputs(payload: dict, mapping_payload: dict) -> None:
    """
    写盘并设只读，随后生成哈希清单
        :param payload: 冻结样本内容
        :param mapping_payload: 映射内容
        """
    # 先清只读，允许 --force 重写
    for path in (OUT_SAMPLE, OUT_MAPPING, OUT_HASH):
        if path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)

    OUT_SAMPLE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    OUT_MAPPING.write_text(json.dumps(mapping_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    # 哈希清单：sha256sum 兼容格式，便于用系统工具复核
    hashes = (
        f"{sha256_file(OUT_SAMPLE)}  {OUT_SAMPLE.name}\n"
        f"{sha256_file(OUT_MAPPING)}  {OUT_MAPPING.name}\n"
    )
    OUT_HASH.write_text(hashes, encoding="utf-8", newline="\n")

    # 冻结后设只读，防误改；要改必须 --force 并升版本
    for path in (OUT_SAMPLE, OUT_MAPPING, OUT_HASH):
        os.chmod(path, stat.S_IREAD)


def read_hash_file() -> dict[str, str]:
    """
    读取 sha256 清单
        :return: 文件名 → 期望哈希
    """
    expected: dict[str, str] = {}  # 文件名 → 哈希
    for line in OUT_HASH.read_text(encoding="utf-8").splitlines():  # 每行 "hash  file"
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)  # 拆分
        expected[name.strip()] = digest  # 记录
    return expected


def verify() -> int:
    """
    校验冻结产物：哈希、内容哈希、重跑抽样、分层数量、泄题扫描、来源漂移
        :return: 0 全部通过；1 有失败
    """
    failures: list[str] = []  # 失败项
    checks: list[str] = []  # 通过项

    if not OUT_SAMPLE.exists() or not OUT_MAPPING.exists() or not OUT_HASH.exists():
        print("FAIL 冻结产物缺失，请先执行生成")
        return 1

    # 1) 文件哈希
    expected_hash = read_hash_file()  # 清单
    for name, path in ((OUT_SAMPLE.name, OUT_SAMPLE), (OUT_MAPPING.name, OUT_MAPPING)):
        actual = sha256_file(path)  # 实际哈希
        if expected_hash.get(name) == actual:
            checks.append(f"哈希一致 {name}")
        else:
            failures.append(f"哈希不一致 {name}")

    payload = json.loads(OUT_SAMPLE.read_text(encoding="utf-8"))  # 冻结内容
    mapping_payload = json.loads(OUT_MAPPING.read_text(encoding="utf-8"))  # 映射
    rows = payload["cases"]  # 60 条
    meta = payload["meta"]  # 元数据

    # 2) 内容哈希
    if sha256_bytes(_canonical(rows)) == meta.get("content_sha256"):
        checks.append("内容哈希一致")
    else:
        failures.append("内容哈希不一致（cases 被改过）")

    # 3) 数量与分层
    if len(rows) == N_TOTAL and len({row["blind_id"] for row in rows}) == N_TOTAL:
        checks.append(f"条数与编号唯一 {N_TOTAL}")
    else:
        failures.append("条数或编号唯一性不符")

    strata: dict[str, int] = {}  # 分层计数
    for item in mapping_payload["mapping"]:
        strata[item["stratum"]] = strata.get(item["stratum"], 0) + 1  # 累加
    if strata == {"high": N_HIGH, "low": N_LOW, "mid": N_MID}:
        checks.append(f"分层数量 {strata}")
    else:
        failures.append(f"分层数量不符：{strata}")

    # 4) 重跑抽样，逐条比对
    rebuilt, rebuilt_mapping = build_payload(meta["seed"], meta["frozen_at"])  # 用原种子/原时间重建
    if rebuilt["cases"] == rows:
        checks.append("重跑抽样与冻结内容一致")
    else:
        failures.append("重跑抽样与冻结内容不一致（来源或算法变了）")
    if rebuilt_mapping["mapping"] == mapping_payload["mapping"]:
        checks.append("重跑映射一致")
    else:
        failures.append("重跑映射不一致")

    # 5) 来源漂移
    current_sources = {item["file"]: item["sha256"] for item in rebuilt["meta"]["sources"]}  # 当前来源哈希
    frozen_sources = {item["file"]: item["sha256"] for item in meta["sources"]}  # 冻结时来源哈希
    if current_sources == frozen_sources:
        checks.append("来源文件未被改动")
    else:
        failures.append("来源文件已变化，冻结失效")

    # 6) 泄题扫描
    problems = scan_leaks(rows)  # 泄题问题
    if not problems:
        checks.append("泄题扫描通过")
    else:
        failures.extend(problems)

    for line in checks:
        print(f"OK   {line}")
    for line in failures:
        print(f"FAIL {line}")
    print(f"RESULT checks={len(checks)} failures={len(failures)}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    """
    命令行入口
        :param argv: 参数列表（None 表示取 sys.argv）
        :return: 退出码
    """
    parser = argparse.ArgumentParser(description="冻结/校验 blind_v2 独立双标样本")
    parser.add_argument("--verify", action="store_true", help="只校验，不写盘")  # 校验模式
    parser.add_argument("--force", action="store_true", help="覆盖已有冻结产物（仅开发期）")  # 强制重写
    parser.add_argument("--seed", default=DEFAULT_SEED, help="预登记种子")  # 种子可覆盖，但默认固定
    args = parser.parse_args(argv)  # 解析

    if args.verify:
        return verify()

    if OUT_SAMPLE.exists() and not args.force:
        print(f"REFUSE 已存在 {OUT_SAMPLE.name}；确需重写请加 --force 并升版本")
        return 2

    payload, mapping_payload = build_payload(args.seed)  # 生成
    write_outputs(payload, mapping_payload)  # 写盘 + 只读
    print(f"frozen n={payload['meta']['n']} strata={payload['meta']['strata']}")
    print(f"sample={OUT_SAMPLE}")
    print(f"mapping={OUT_MAPPING}")
    print(f"sha256={OUT_HASH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
