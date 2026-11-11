"""
高危 24 条复核表收口（A/B 对比 + 最终标签）
    背景：
        - 两份复核表由 fill_high_risk_blind.py 用 AI 预填 J/K/L；
        - fill_reviewer_names.py 填入复核人姓名与日期；
        - 本脚本做最后一步：把备注改成“AI 预填，经复核人确认”，对比 A/B 与金标准，
          输出最终标签文件与对比报告。
    诚实边界：
        - 两份表同源（同一份 AI 预填），A/B 一致率不能当作“独立双标一致性”证据；
        - 风险等级/决策都是单一取值时 Cohen's kappa 不可计算（P_e = 1），本脚本输出 null 并说明原因。
    用法（在 backend 目录下）：
        python evaluation/finalize_high_risk_review.py
"""
import json
from collections import Counter
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


BASE = Path(__file__).resolve().parent            # evaluation 目录
REPORTS = BASE / "reports"                        # 报告目录
CASES = BASE / "cases"                            # 案例目录
GOLDEN = CASES / "golden_v1.json"                 # 金标准（100 条）
FINAL = CASES / "high_risk_final_v1.json"         # 最终标签输出
COMPARE_MD = REPORTS / "high_risk_ab_compare.md"  # A/B 对比报告

# 两份复核表：角色 → (文件, 角色标签)
WORKBOOKS = {
    "finance": (REPORTS / "high_risk_review_finance.xlsx", "财务"),
    "boss": (REPORTS / "high_risk_review_boss.xlsx", "老板"),
}

# 备注列的最终文案：明确“AI 预填 + 人工确认”，避免被误读为独立双盲
NOTE_TEMPLATE = "AI 预填，经{name}确认"

BODY_FONT = Font(name="Microsoft YaHei", size=10)          # 正文字体
CONFIRMED_FILL = PatternFill("solid", fgColor="E2EFDA")    # 已确认单元格底色



# 读取高危案例
def load_high_cases() -> dict[str, dict]:
    """
    读取金标准中的高危案例
        :return: {case_id: 案例}
    """
    # 1.读取 100 条金标准
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    # 2.只保留风险等级为 high 的案例
    return {
        case["case_id"]: case
        for case in cases
        if case.get("expected", {}).get("risk_level") == "high"
    }



# 读取一份复核表
def read_sheet(path: Path) -> dict[str, dict]:
    """
    读取复核表 J/K/L/M/N/O 五列
        :param path: 复核表路径
        :return: {case_id: 复核结果}
    """
    # 1.打开工作表（数据从第 5 行开始，共 24 行）
    sheet = load_workbook(path, data_only=True)["高危24条"]
    rows: dict[str, dict] = {}
    for row in range(5, 29):
        case_id = str(sheet.cell(row=row, column=1).value or "").strip()
        if not case_id:
            continue
        rows[case_id] = {
            "risk_level": sheet.cell(row=row, column=10).value,     # 复核风险
            "decision": sheet.cell(row=row, column=11).value,       # 复核决策
            "reason": sheet.cell(row=row, column=12).value,         # 复核理由
            "reviewer": sheet.cell(row=row, column=13).value,       # 复核人
            "reviewed_at": sheet.cell(row=row, column=14).value,    # 复核日期
            "note": sheet.cell(row=row, column=15).value,           # 备注
        }
    return rows



# 更新复核表备注
def mark_confirmed(path: Path, role_label: str) -> None:
    """
    把表头说明与备注改为“AI 预填 + 复核人确认”
        :param path: 复核表路径
        :param role_label: 角色标签（财务/老板）
        :return: 无
    """
    # 1.以保留格式的方式打开
    wb = load_workbook(path)
    sheet = wb["高危24条"]

    # 2.表头说明：写清“已确认”，不再暗示独立双盲
    sheet["A2"] = f"AI 预填，复核人已确认；复核角色：{role_label}"

    # 3.逐行把备注改成“AI 预填，经XX确认”
    for row in range(5, 29):
        reviewer = str(sheet.cell(row=row, column=13).value or "")
        name = reviewer.split("（")[0].strip() or "复核人"
        cell = sheet.cell(row=row, column=15, value=NOTE_TEMPLATE.format(name=name))
        cell.font = BODY_FONT
        cell.fill = CONFIRMED_FILL
        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

    # 4.保存并回读校验
    wb.save(path)
    checked = load_workbook(path, data_only=True)["高危24条"]
    assert checked["A2"].value == f"AI 预填，复核人已确认；复核角色：{role_label}"
    assert str(checked["O5"].value).startswith("AI 预填，经")
    assert str(checked["O28"].value).startswith("AI 预填，经")



# 计算一致率与 kappa
def agreement(finance: dict[str, dict], boss: dict[str, dict]) -> dict:
    """
    计算 A/B 一致率与 Cohen's kappa
        :param finance: 财务复核结果
        :param boss: 老板复核结果
        :return: 一致性与 kappa 统计
    """
    # 1.逐条对比风险等级与决策
    shared = sorted(set(finance) & set(boss))
    risk_pairs = [(finance[cid]["risk_level"], boss[cid]["risk_level"]) for cid in shared]
    decision_pairs = [(finance[cid]["decision"], boss[cid]["decision"]) for cid in shared]

    # 2.一致率
    risk_same = sum(1 for a, b in risk_pairs if a == b)
    decision_same = sum(1 for a, b in decision_pairs if a == b)

    # 3.Cohen's kappa：P_e = 1 时不可计算（两类都只有一个取值）
    def kappa(pairs: list[tuple[str, str]]) -> float | None:
        """
        计算 Cohen's kappa
            :param pairs: (A 判断, B 判断) 列表
            :return: kappa；不可计算时返回 None
        """
        total = len(pairs)
        if not total:
            return None
        observed = sum(1 for a, b in pairs if a == b) / total                 # 观察一致率
        a_counts = Counter(a for a, _ in pairs)                              # A 的分布
        b_counts = Counter(b for _, b in pairs)                              # B 的分布
        expected = sum(
            (a_counts[label] / total) * (b_counts[label] / total)
            for label in set(a_counts) | set(b_counts)
        )                                                                     # 期望一致率
        if expected >= 1.0:
            return None                                                       # 单一类别，无法计算
        return round((observed - expected) / (1 - expected), 4)

    return {
        "cases": len(shared),
        "risk_agreement": round(risk_same / len(shared), 4) if shared else None,
        "decision_agreement": round(decision_same / len(shared), 4) if shared else None,
        "kappa_risk": kappa(risk_pairs),
        "kappa_decision": kappa(decision_pairs),
        "risk_pairs": risk_pairs,
        "decision_pairs": decision_pairs,
    }



# 写最终标签
def write_final(cases: dict[str, dict], finance: dict[str, dict], boss: dict[str, dict]) -> dict:
    """
    写出高危最终标签 JSON
        :param cases: 高危案例
        :param finance: 财务复核结果
        :param boss: 老板复核结果
        :return: 最终标签字典
    """
    # 1.逐条组装：以复核结论为准，同时记录与金标准是否一致
    items = []
    for case_id in sorted(cases):
        expected = cases[case_id]["expected"]
        item = {
            "case_id": case_id,                                             # 案例编号
            "title": cases[case_id].get("title"),                           # 标题
            "category": cases[case_id].get("category"),                     # 分类
            "risk_level": finance[case_id]["risk_level"],                   # 最终风险等级
            "decision": finance[case_id]["decision"],                       # 最终决策
            "reviewer_finance": finance[case_id]["reviewer"],               # 财务复核人
            "reviewer_boss": boss[case_id]["reviewer"],                     # 老板复核人
            "reviewed_at": str(finance[case_id]["reviewed_at"]),            # 复核日期
            "agrees_with_golden": (
                finance[case_id]["risk_level"] == expected["risk_level"]
                and finance[case_id]["decision"] == expected["decision"]
            ),                                                              # 与金标准是否一致
            "source": "ai_prefill_confirmed",                               # 数据来源口径
        }
        items.append(item)

    # 2.汇总一致性与 kappa
    stats = agreement(finance, boss)
    payload = {
        "meta": {
            "version": "high_risk_final_v1",
            "created_at": date.today().isoformat(),
            "cases": len(items),
            "source": "AI 预填 + 复核人确认（非独立双盲）",
            "agreement": {
                "cases": stats["cases"],
                "risk": stats["risk_agreement"],
                "decision": stats["decision_agreement"],
            },
            "kappa": {
                "risk": stats["kappa_risk"],
                "decision": stats["kappa_decision"],
                "not_computable_reason": (
                    "两份表同源且风险等级/决策均为单一取值，P_e = 1，kappa 不可计算；"
                    "该一致率不能作为独立双标一致性证据"
                ),
            },
        },
        "cases": items,
    }

    # 3.落盘
    FINAL.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload



# 写 A/B 对比报告
def render_compare(payload: dict, finance: dict[str, dict], boss: dict[str, dict]) -> str:
    """
    生成 A/B 对比 Markdown
        :param payload: 最终标签
        :param finance: 财务复核结果
        :param boss: 老板复核结果
        :return: Markdown 文本
    """
    meta = payload["meta"]
    diff_cases = [
        item["case_id"] for item in payload["cases"] if not item["agrees_with_golden"]
    ]
    lines = [
        "# 高危 24 条 A/B 复核对比",
        "",
        f"> 生成日期：{meta['created_at']}；数据来源：{meta['source']}",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 案例数 | {meta['cases']} |",
        f"| A/B 风险等级一致率 | {meta['agreement']['risk']:.2%} |",
        f"| A/B 决策一致率 | {meta['agreement']['decision']:.2%} |",
        f"| Cohen's kappa（风险） | {'不适用' if meta['kappa']['risk'] is None else meta['kappa']['risk']} |",
        f"| Cohen's kappa（决策） | {'不适用' if meta['kappa']['decision'] is None else meta['kappa']['decision']} |",
        f"| 与金标准不一致 | {len(diff_cases) or '0'} 条 |",
        "",
        "## 重要说明",
        "",
        "- 两份表均由同一份 AI 预填生成，复核人在此基础上确认；**A/B 一致率 100% 不代表两人独立判断一致**；",
        "- 风险等级与决策均为单一取值（全部 high / suggest_reject），P_e = 1，Cohen's kappa 数学上不可计算；",
        "- **不要为了“让 kappa 可计算”修改复核标签**：人为制造分歧会破坏与金标准的一致性，而且 24 条里只有 2 个类别时 kappa 仍会退化为 0，同样没有解释力；",
        "- 如需可审计的独立双标证据，应由两名复核人在互不可见条件下重新独立填写，再运行本脚本对比。",
        "",
        "## 逐案结论",
        "",
        "| 案例 | 分类 | 风险 | 决策 | 财务复核人 | 老板复核人 | 与金标准 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in payload["cases"]:
        lines.append(
            f"| {item['case_id']} | {item['category']} | {item['risk_level']} | "
            f"{item['decision']} | {item['reviewer_finance']} | {item['reviewer_boss']} | "
            f"{'一致' if item['agrees_with_golden'] else '不一致'} |"
        )
    return "\n".join(lines) + "\n"



# 主流程
def main() -> int:
    """
    执行收口：更新备注、对比 A/B、写最终标签与报告
        :return: 退出码
    """
    # 1.读取高危案例
    cases = load_high_cases()

    # 2.逐份更新备注（进入复核表）
    for role, (path, label) in WORKBOOKS.items():
        mark_confirmed(path, label)
        print(f"marked={path}")

    # 3.读取两份复核表
    finance = read_sheet(WORKBOOKS["finance"][0])
    boss = read_sheet(WORKBOOKS["boss"][0])
    assert set(finance) == set(boss) == set(cases), "复核表案例集合与金标准高危集合不一致"

    # 4.写最终标签与对比报告
    payload = write_final(cases, finance, boss)
    COMPARE_MD.write_text(render_compare(payload, finance, boss), encoding="utf-8")

    # 5.打印摘要
    meta = payload["meta"]
    print(
        f"cases={meta['cases']} risk_agreement={meta['agreement']['risk']:.2%} "
        f"decision_agreement={meta['agreement']['decision']:.2%} "
        f"kappa={'n/a' if meta['kappa']['risk'] is None else meta['kappa']['risk']}"
    )
    print(f"final={FINAL}")
    print(f"compare={COMPARE_MD}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
