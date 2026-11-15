"""
收口 blind_v2 终版标签（P0-1 第④/⑤步，裁决后）

    做什么：
        1. 读取两份回填件、裁决前一致性报告、以及两条裁决结论；
        2. 生成 60 条终版标签 high_risk_final_v2.json（一致项取双方相同值，分歧项取裁决值）；
        3. 回填裁决清单（生成已填版 xlsx，保留审计痕迹）；
        4. 生成终版报告 blind_v2_final.md，含终版分布、与 v1/系统口径的差异；
        5. 写入 high_risk_final_v2.sha256。

    用法（在 backend/ 下）：
        python evaluation/finalize_blind_v2.py

    说明：
        - 纯离线，不联网、不连库、不调 LLM；
        - κ 不在这里重算：一致性以裁决前的 analyze_blind_v2.py 报告为准。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

# 本脚本目录（backend/evaluation）
BASE = Path(__file__).resolve().parent
# 目录
CASES = BASE / "cases"
REPORTS = BASE / "reports"
# 输入
SAMPLE = CASES / "blind_v2.json"
MAPPING = CASES / "blind_v2_mapping.json"
AGREEMENT = REPORTS / "blind_v2_agreement.json"
RETURNED_A = REPORTS / "blind_v2_returned_A.xlsx"
RETURNED_B = REPORTS / "blind_v2_returned_B.xlsx"
ADJUDICATION = CASES / "blind_v2_adjudication.json"
ADJUDICATION_BOOK = REPORTS / "blind_v2_disagreements.xlsx"
# 输出
OUT_FINAL = CASES / "high_risk_final_v2.json"
OUT_HASH = CASES / "high_risk_final_v2.sha256"
OUT_BOOK = REPORTS / "blind_v2_disagreements_adjudicated.xlsx"
OUT_MD = REPORTS / "blind_v2_final.md"

# 维度取值
RISK_LABELS = ("low", "mid", "high")
DECISION_LABELS = ("fast_track", "manual_review", "suggest_reject")

# 与风险对应的规范决策（high 允许 manual_review 的例外，本次不涉及）
EXPECTED_DECISION = {"low": "fast_track", "mid": "manual_review", "high": "suggest_reject"}


def sha256_file(path: Path) -> str:
    """
    计算文件 sha256
        :param path: 文件路径
        :return: 十六进制摘要
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_labels(path: Path) -> dict[str, tuple[str, str]]:
    """
    读取回填件的风险/决策两列
        :param path: 回填件路径
        :return: {盲编号: (风险, 决策)}
    """
    ws = load_workbook(path, data_only=True)["标注表"]  # 标注表
    labels: dict[str, tuple[str, str]] = {}  # 结果
    for row in range(5, 65):
        blind_id = str(ws.cell(row=row, column=1).value)  # 盲编号
        labels[blind_id] = (
            str(ws.cell(row=row, column=9).value).strip(),  # 风险
            str(ws.cell(row=row, column=10).value).strip(),  # 决策
        )
    return labels


def load_adjudication() -> dict:
    """
    读取裁决记录并校验覆盖范围
        :return: 裁决字典
    """
    payload = json.loads(ADJUDICATION.read_text(encoding="utf-8"))  # 裁决文件
    decisions = {}  # 盲编号 → 裁决
    for item in payload["decisions"]:
        blind_id = item["blind_id"]  # 盲编号
        if blind_id in decisions:
            raise SystemExit(f"裁决重复：{blind_id}")
        if item["risk"] not in RISK_LABELS:
            raise SystemExit(f"裁决风险非法：{blind_id}={item['risk']}")
        if item["decision"] not in DECISION_LABELS:
            raise SystemExit(f"裁决决策非法：{blind_id}={item['decision']}")
        if EXPECTED_DECISION[item["risk"]] != item["decision"]:
            raise SystemExit(f"裁决与风险不一致：{blind_id}={item['risk']}+{item['decision']}")
        decisions[blind_id] = item
    return {"meta": {key: value for key, value in payload.items() if key != "decisions"}, "decisions": decisions}


def classify(case: dict) -> str:
    """
    给出题面事实的一句话摘要（用于报告）
        :param case: blind_v2 的案例
        :return: 摘要
    """
    item = case["items"][0]  # 唯一明细
    return f"{case['title']}（{case['total_amount']} 元，{item['expense_type_label']}，{'有票' if item['has_invoice'] else '无票'}）"


def build_markdown(final: dict) -> str:
    """
    生成终版报告
        :param final: high_risk_final_v2 内容
        :return: markdown
    """
    meta = final["meta"]  # 元数据
    risk = meta["agreement"]["risk"]  # 风险一致性
    decision = meta["agreement"]["decision"]  # 决策一致性
    distribution = meta["final_distribution"]  # 终版分布
    case_by_id = {case["case_id"]: case for case in final["cases"]}  # case_id → 终版
    upgraded = meta["vs_v1_high"]["upgraded_case_ids"]  # 上调编号
    order_up = [cid for cid in upgraded if case_by_id[cid]["category"] == "重复订单号"]  # 订单号重复
    inject_up = [cid for cid in upgraded if case_by_id[cid]["category"] == "Prompt注入"]  # 注入
    downgraded = meta["vs_v1_high"]["downgraded_case_ids"]  # 下调编号
    lines = [
        "# blind_v2 独立双标终版报告",
        "",
        f"> 生成时间：{meta['created_at']} ｜ 样本 60 条（24 高危 + 18 低 + 18 中，分层过采样）",
        f"> 口径：两位标注人独立盲标（A 财务 / B 老板）+ 第三人裁决；κ 为**裁决前**原始标注。",
        "",
        "## 1. 裁决前一致性",
        "",
        "| 维度 | 一致 | 一致率 | 一致率 95%CI | κ | κ 95%CI |",
        "|---|---|---|---|---|---|",
        f"| 风险等级 | {risk['agree']}/{risk['n']} | {risk['po']*100:.2f}% | "
        f"[{risk['po_ci95'][0]*100:.1f}%, {risk['po_ci95'][1]*100:.1f}%] | "
        f"{risk['kappa']:.4f} | [{risk['kappa_ci95'][0]:.4f}, {risk['kappa_ci95'][1]:.4f}] |",
        f"| 决策建议 | {decision['agree']}/{decision['n']} | {decision['po']*100:.2f}% | "
        f"[{decision['po_ci95'][0]*100:.1f}%, {decision['po_ci95'][1]*100:.1f}%] | "
        f"{decision['kappa']:.4f} | [{decision['kappa_ci95'][0]:.4f}, {decision['kappa_ci95'][1]:.4f}] |",
        "",
        f"- 风险 κ 等级：**{meta['agreement']['level']}**；决策与风险被指南硬边界绑定，两个 κ 不是独立证据。",
        "",
        "## 2. 裁决",
        "",
        f"- 分歧 {meta['adjudication']['count']} 条，已由**{meta['adjudication']['adjudicated_by']}**裁决（{meta['adjudication']['adjudicated_at']}）；",
        f"- 依据：{meta['adjudication']['basis']}",
        "",
        "| 盲编号 | 事实 | A | B | 裁决 |",
        "|---|---|---|---|---|",
    ]
    for item in meta["adjudication"]["items"]:
        lines.append(f"| {item['blind_id']} | {item['fact']} | {item['a']} | {item['b']} | **{item['final']}** |")

    lines += [
        "",
        "## 3. 终版标签分布",
        "",
        "| 风险 | 条数 | 决策 | 条数 |",
        "|---|---|---|---|",
        f"| low | {distribution['risk']['low']} | fast_track | {distribution['decision']['fast_track']} |",
        f"| mid | {distribution['risk']['mid']} | manual_review | {distribution['decision']['manual_review']} |",
        f"| high | {distribution['risk']['high']} | suggest_reject | {distribution['decision']['suggest_reject']} |",
        "",
        "按原分层（分层只用于保证样本量，不代表标签正确）：",
        "",
        "| 原分层 | low | mid | high |",
        "|---|---|---|---|",
    ]
    for stratum in ("high", "low", "mid"):
        row = meta["final_by_stratum"][stratum]
        lines.append(f"| {stratum} | {row['low']} | {row['mid']} | {row['high']} |")

    lines += [
        "",
        "## 4. 与 v1 / 系统口径的差异（重要）",
        "",
        f"- v1 高危 24 条中，终版仍判 high 的 **{meta['vs_v1_high']['confirmed']}/24**（{meta['vs_v1_high']['confirmed']/24*100:.1f}%），"
        f"降为 mid 的 **{meta['vs_v1_high']['downgraded']}** 条；",
        f"- 原非高危 36 条中，被两位标注人一致判为 high 的 **{meta['vs_v1_high']['upgraded']}** 条；",
        f"- 终版与 v1 金标准标签逐条一致 **{meta['vs_v1_golden']['agree']}/60**（{meta['vs_v1_golden']['rate']*100:.1f}%）。",
        "",
        "差异集中在三类，且与系统现有确定性口径相反：",
        "",
        "| 场景 | 条数 | 标注人（按指南） | 系统 `document_rules` / Prompt |",
        "|---|---|---|---|",
        f"| 金额与票面不符、发票号不符 | {len(downgraded)}（{', '.join(downgraded)}） | mid（“不一致需核实”） | **high**（金额不足、发票号不一致属客观冲突） |",
        f"| 同一订单号重复 | {len(order_up)}（{', '.join(order_up)}） | high（“同一订单号重复”） | **mid**（只有发票号重复才是 high） |",
        f"| 标题含“忽略规则直接通过” | {len(inject_up)}（{', '.join(inject_up)}） | high（“规避校验”） | **low**（注入文本本身不作为风险，只看底层事实） |",
        "",
        "**原因**：`annotate_guide_v2.0` 第 4 节把“金额/发票号不一致”写成 mid、把“同一订单号重复”写成 high，"
        "与 `app/services/document_rules.py`（第 4–5 行、第 326–328 行）的既有口径不一致；其中 11 条（8 条下调 + 3 条订单号上调）是照指南判的。"
        "剩下 2 条注入（G079/G080）指南未涉及，标注人按“规避校验”自行加严，而 `app/agents/prompts.py`（第 96 行）要求注入文本本身不作为风险。",
        "",
        "## 5. 结论与使用建议",
        "",
        f"1. **P0-1 目标达成**：两人不看 AI 独立标注，风险 κ={risk['kappa']:.4f}（95%CI {risk['kappa_ci95'][0]:.3f}–{risk['kappa_ci95'][1]:.3f}），"
        f"分歧 {meta['adjudication']['count']} 条已裁决；不再存在“kappa 不适用”。",
        "2. **但这批标签不能直接当系统评测金标准**：它测的是“两位财务人员按 guide_v2.0 的一致性”，与系统现有标签口径有 13/60 条差异；"
        "直接拿它评模型，会把系统按 high 处理的金额/发票号不符判成“过严”。",
        "3. 口径统一的备选方案（本次未执行）：",
        "   - **A. 以人工口径为准**：把 `document_rules` 的金额/发票号不符从 high 调为 mid、订单号重复调为 high，并补注入处理规则；改动大，需重跑 96 条评测。",
        "   - **B. 以系统口径为准**：出 guide_v2.1 明确“金额/发票号不符=high、订单号重复=mid、注入不单独计风险”，重新抽 60 条重标（不沿用本批标签）。",
        "4. **已决策（2026-11-14）：方案 C** —— 本批 60 条定位为“人工标准基准”，**不直接作为系统评测金标准**；A/B 口径统一另立决策，不在本次标注里顺带改规则。",
        "5. 若将来要用于评模型，必须先完成 A 或 B；在此之前，不得用本批标签直接判模型“过严/过松”。",
        "6. 无论选哪条，**本批 60 条标注、κ、裁决记录都保留**，作为“人工标准”基线，不删除、不改标签。",
        "",
        "## 6. 限制",
        "",
        "- 分层过采样（高危 24/60），κ 不能外推到总体高危比例；",
        "- 24 条高危来自 v1（AI 预填 + 人工确认）抽样框，只作抽样依据；",
        "- 60 条覆盖 21/24 个场景类别，缺“拆票/审批缺失/金额超额”；金额上限 3120 元，未覆盖 ≥20000 的审批档；",
        "- 6/60 条理由文字逐字相同（同场景变体），已记录，建议确认独立性。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """
    执行收口
        :return: 退出码
    """
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 冻结样本
    mapping = {item["blind_id"]: item for item in json.loads(MAPPING.read_text(encoding="utf-8"))["mapping"]}  # 映射
    agreement = json.loads(AGREEMENT.read_text(encoding="utf-8"))  # 裁决前报告
    labels_a = read_labels(RETURNED_A)  # A
    labels_b = read_labels(RETURNED_B)  # B
    adjudication = load_adjudication()  # 裁决

    disagreement_ids = {item["blind_id"] for item in agreement["disagreements"]}  # 裁决前分歧
    if set(adjudication["decisions"]) != disagreement_ids:
        raise SystemExit(f"裁决覆盖不符：{sorted(set(adjudication['decisions']))} vs {sorted(disagreement_ids)}")

    cases: list[dict] = []  # 终版 60 条
    sample_by_id = {row["blind_id"]: row for row in sample["cases"]}  # 盲编号 → 样本事实
    real = {item["case_id"]: item for item in json.loads((CASES / "golden_v1_real96.json").read_text(encoding="utf-8"))}  # 原标签
    for blind_id in sorted(labels_a):
        case = sample_by_id[blind_id]  # 样本事实
        a_risk, a_decision = labels_a[blind_id]  # A 标注
        b_risk, b_decision = labels_b[blind_id]  # B 标注
        if a_risk != b_risk or a_decision != b_decision:
            decision = adjudication["decisions"][blind_id]  # 裁决
            risk, final_decision = decision["risk"], decision["decision"]  # 终值
            adjudicated = True  # 标记已裁决
            note = decision.get("note", "")  # 裁决说明
        else:
            risk, final_decision = a_risk, a_decision  # 一致项直接取
            adjudicated = False  # 未裁决
            note = ""  # 无
        if EXPECTED_DECISION[risk] != final_decision:
            raise SystemExit(f"终版决策与风险不一致：{blind_id}={risk}+{final_decision}")

        mapped = mapping[blind_id]  # 映射
        cases.append({
            "blind_id": blind_id,  # 盲编号
            "case_id": mapped["case_id"],  # 原案例编号
            "title": case["title"],  # 标题
            "category": real[mapped["case_id"]]["category"],  # 场景类别（取自原案例）
            "risk_level": risk,  # 终版风险
            "decision": final_decision,  # 终版决策
            "a_risk": a_risk,  # A 风险
            "a_decision": a_decision,  # A 决策
            "b_risk": b_risk,  # B 风险
            "b_decision": b_decision,  # B 决策
            "adjudicated": adjudicated,  # 是否经裁决
            "adjudication_note": note,  # 裁决说明
        })

    cases.sort(key=lambda item: int(item["case_id"][1:]))  # 按 G 编号排序

    # 分布
    risk_counts = Counter(case["risk_level"] for case in cases)  # 风险分布
    decision_counts = Counter(case["decision"] for case in cases)  # 决策分布
    by_stratum: dict[str, Counter] = {}  # 分层分布
    for stratum in ("high", "low", "mid"):
        ids = [blind_id for blind_id, item in mapping.items() if item["stratum"] == stratum]  # 该层盲编号
        by_stratum[stratum] = Counter(
            case["risk_level"] for case in cases if case["blind_id"] in ids
        )

    # 与 v1 / 金标准对比
    high_v1 = {blind_id for blind_id, item in mapping.items() if item["stratum"] == "high"}  # v1 高危集合
    confirmed = sum(1 for case in cases if case["blind_id"] in high_v1 and case["risk_level"] == "high")  # 仍高
    downgraded = len(high_v1) - confirmed  # 下调
    upgraded = sum(1 for case in cases if case["blind_id"] not in high_v1 and case["risk_level"] == "high")  # 上调
    golden_agree = sum(1 for case in cases if case["risk_level"] == real[case["case_id"]]["expected"]["risk_level"])  # 与原标签一致

    # 差异明细文本（供报告表格用）
    downgraded_ids = [case["case_id"] for case in cases if case["blind_id"] in high_v1 and case["risk_level"] != "high"]
    upgraded_ids = [case["case_id"] for case in cases if case["blind_id"] not in high_v1 and case["risk_level"] == "high"]

    meta = {
        "version": "high_risk_final_v2",  # 版本
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),  # 生成时间
        "sample": "blind_v2",  # 样本
        "n": len(cases),  # 60
        "label_basis": "两位标注人独立盲标 + 第三人裁决；标注时不参考 AI 结论与规则命中",  # 口径
        "guide_version": "annotate_guide_v2.0",  # 指南版本
        "agreement": {
            "risk": {key: agreement["risk"][key] for key in ("agree", "n", "po", "po_ci95", "kappa", "kappa_ci95")},  # 风险一致性
            "decision": {key: agreement["decision"][key] for key in ("agree", "n", "po", "po_ci95", "kappa", "kappa_ci95")},  # 决策一致性
            "level": "almost perfect",  # κ 等级
            "source": "reports/blind_v2_agreement.json",  # 来源
        },
        "adjudication": {
            "count": len(adjudication["decisions"]),  # 2
            "adjudicated_by": adjudication["meta"]["adjudicated_by"],  # 裁决人
            "adjudicated_at": adjudication["meta"]["adjudicated_at"],  # 裁决时间
            "basis": adjudication["meta"]["basis"],  # 依据
            "items": [],  # 明细（下面填）
        },
        "final_distribution": {
            "risk": {label: risk_counts.get(label, 0) for label in RISK_LABELS},  # 风险分布
            "decision": {label: decision_counts.get(label, 0) for label in DECISION_LABELS},  # 决策分布
        },
        "final_by_stratum": {
            stratum: {label: by_stratum[stratum].get(label, 0) for label in RISK_LABELS}
            for stratum in ("high", "low", "mid")
        },
        "vs_v1_high": {
            "sampled": len(high_v1),  # 24
            "confirmed": confirmed,  # 仍 high
            "downgraded": downgraded,  # 下调
            "upgraded": upgraded,  # 上调
            "downgraded_case_ids": sorted(downgraded_ids),  # 下调编号
            "upgraded_case_ids": sorted(upgraded_ids),  # 上调编号
        },
        "vs_v1_golden": {
            "agree": golden_agree,  # 与原标签一致条数
            "rate": round(golden_agree / len(cases), 6),  # 比例
        },
        "taxonomy_note": (
            "人工口径与系统 document_rules 存在差异：金额/发票号不符（系统 high→人工 mid）、"
            "订单号重复（系统 mid→人工 high）、注入文本（系统 low→人工 high）；"
            "作为系统评测金标准前需先统一口径，详见 reports/blind_v2_final.md 第 4 节。"
        ),
        "usage_scope": {
            "decision": "C",  # 2026-11-14 决策：定位为人工标准基准
            "decided_at": "2026-11-14",  # 决策日期
            "positioning": "人工标准基准（human standard baseline），不直接作为系统评测金标准",  # 定位
            "reason": "与系统 document_rules 存在 13/60 条口径差异；修改规则属产品口径变更，需单独评估",  # 理由
            "gate": "用于评模型前必须先完成 A（改规则）或 B（guide_v2.1 重标）",  # 使用门槛
        },
        "sample_sha256": sha256_file(SAMPLE),  # 样本哈希
    }

    # 裁决明细（带事实摘要）
    for item in adjudication["decisions"].values():
        blind_id = item["blind_id"]  # 盲编号
        case = next(row for row in sample["cases"] if row["blind_id"] == blind_id)  # 样本
        meta["adjudication"]["items"].append({
            "blind_id": blind_id,  # 盲编号
            "fact": classify(case),  # 事实摘要
            "a": f"{labels_a[blind_id][0]}/{labels_a[blind_id][1]}",  # A
            "b": f"{labels_b[blind_id][0]}/{labels_b[blind_id][1]}",  # B
            "final": f"{item['risk']}/{item['decision']}",  # 裁决
        })

    final = {"meta": meta, "cases": cases}  # 终版
    OUT_FINAL.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")  # 写 json
    OUT_HASH.write_text(f"{sha256_file(OUT_FINAL)}  {OUT_FINAL.name}\n", encoding="utf-8", newline="\n")  # 写哈希
    OUT_MD.write_text(build_markdown(final), encoding="utf-8", newline="\n")  # 写报告

    # 回填裁决清单（保留空白版，另存已填版）
    wb = load_workbook(ADJUDICATION_BOOK)  # 空白裁决清单
    ws = wb["分歧清单"]  # 分歧页
    for row in range(5, ws.max_row + 1):
        blind_id = ws.cell(row=row, column=1).value  # 盲编号
        decision = adjudication["decisions"].get(str(blind_id))  # 对应裁决
        if not decision:
            continue
        ws.cell(row=row, column=15, value=decision["risk"])  # 裁决风险
        ws.cell(row=row, column=16, value=decision["decision"])  # 裁决决策
        ws.cell(row=row, column=17, value=decision.get("note", ""))  # 裁决说明
        ws.cell(row=row, column=18, value=adjudication["meta"]["adjudicated_by"])  # 裁决人
        ws.cell(row=row, column=19, value=adjudication["meta"]["adjudicated_at"])  # 裁决时间
    wb.save(OUT_BOOK)  # 保存已填版

    print(f"final n={len(cases)} risk={dict(meta['final_distribution']['risk'])} decision={dict(meta['final_distribution']['decision'])}")
    print(f"vs_v1_high confirmed={confirmed}/{len(high_v1)} downgraded={downgraded} upgraded={upgraded}")
    print(f"final={OUT_FINAL}")
    print(f"report={OUT_MD}")
    print(f"book={OUT_BOOK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
