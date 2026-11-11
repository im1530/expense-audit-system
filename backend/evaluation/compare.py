"""
评测报告对比（Phase F：P0 #2 Prompt/模型版本对比）
    输入：
        - baseline 报告 JSON 与 candidate 报告 JSON（evaluate.py 输出）；
        - 两份报告必须使用同一套案例（case_id 对齐）。
    输出：
        - 指标差值；
        - 逐案例变化（改进 / 退化 / 持平）；
        - Markdown 对比报告。
    边界：只做纯计算，不调用模型、不读数据库。
"""
from typing import Any



# 参与对比的指标
METRIC_KEYS = (
    ("decision_accuracy", "决策一致率", True),
    ("risk_level_accuracy", "风险等级一致率", True),
    ("high_risk_miss_rate", "高危漏放率", False),
    ("finding_recall", "finding 召回率", True),
    ("fallback_rate", "降级率", False),
    ("avg_latency_ms", "平均耗时（ms）", False),
    ("avg_tokens", "平均 token", False),
)



# 按 case_id 建索引
def _index(report: dict) -> dict[str, dict]:
    """
    把报告结果按 case_id 建索引
        :param report: 评测报告
        :return: {case_id: result}
    """
    return {item["case_id"]: item for item in report.get("results") or []}



# 判断单案例是否更好
def _is_better(candidate: dict, baseline: dict) -> bool:
    """
    判断候选结果是否比基线更好
        优先看决策/风险是否正确，再看缺失 finding 数量。
        :param candidate: 候选结果
        :param baseline: 基线结果
        :return: 更好返回 True
    """
    candidate_score = (
        int(bool(candidate.get("decision_match")))
        + int(bool(candidate.get("risk_match")))
        - len(candidate.get("missing_findings") or [])
    )
    baseline_score = (
        int(bool(baseline.get("decision_match")))
        + int(bool(baseline.get("risk_match")))
        - len(baseline.get("missing_findings") or [])
    )
    return candidate_score > baseline_score



# 对比两份报告
def compare_reports(baseline: dict, candidate: dict) -> dict:
    """
    对比 baseline 与 candidate 报告
        :param baseline: 基线报告
        :param candidate: 候选报告
        :return: 对比结果
    """
    baseline_index = _index(baseline)
    candidate_index = _index(candidate)
    common_ids = sorted(set(baseline_index) & set(candidate_index))

    metrics = {}
    for key, label, higher_is_better in METRIC_KEYS:
        old = baseline.get(key, 0)
        new = candidate.get(key, 0)
        delta = round(float(new) - float(old), 4)
        metrics[key] = {
            "label": label,
            "baseline": old,
            "candidate": new,
            "delta": delta,
            "improved": delta > 0 if higher_is_better else delta < 0,
        }

    changes = []
    improved = 0
    regressed = 0
    for case_id in common_ids:
        old = baseline_index[case_id]
        new = candidate_index[case_id]
        changed = (
            old.get("actual_decision") != new.get("actual_decision")
            or old.get("actual_risk_level") != new.get("actual_risk_level")
            or set(old.get("missing_findings") or []) != set(new.get("missing_findings") or [])
        )
        if not changed:
            continue
        better = _is_better(new, old)
        if better:
            improved += 1
        else:
            regressed += 1
        changes.append({
            "case_id": case_id,
            "title": new.get("title") or old.get("title"),
            "baseline_decision": old.get("actual_decision"),
            "candidate_decision": new.get("actual_decision"),
            "baseline_risk": old.get("actual_risk_level"),
            "candidate_risk": new.get("actual_risk_level"),
            "baseline_missing": old.get("missing_findings") or [],
            "candidate_missing": new.get("missing_findings") or [],
            "better": better,
        })

    return {
        "total": len(common_ids),
        "baseline_mode": baseline.get("mode", ""),
        "candidate_mode": candidate.get("mode", ""),
        "metrics": metrics,
        "improved_cases": improved,
        "regressed_cases": regressed,
        "changes": changes,
    }



# Markdown 对比报告
def render_comparison_markdown(comparison: dict) -> str:
    """
    渲染 Markdown 对比报告
        :param comparison: compare_reports 结果
        :return: Markdown 文本
    """
    lines = [
        "# AI 预审版本对比",
        "",
        f"> 对比案例数：{comparison['total']}；改进 {comparison['improved_cases']} 条，"
        f"退化 {comparison['regressed_cases']} 条。",
        "",
        "| 指标 | 基线 | 候选 | 差值 |",
        "|---|---|---|---|",
    ]
    for item in comparison["metrics"].values():
        lines.append(
            f"| {item['label']} | {item['baseline']} | {item['candidate']} | {item['delta']} |"
        )

    lines.extend([
        "",
        "## 变化案例",
        "",
        "| 案例 | 基线决策 | 候选决策 | 基线风险 | 候选风险 | 缺失 finding | 结论 |",
        "|---|---|---|---|---|---|---|",
    ])
    for item in comparison["changes"]:
        lines.append(
            f"| {item['case_id']} {item['title']} | {item['baseline_decision']} | "
            f"{item['candidate_decision']} | {item['baseline_risk']} | {item['candidate_risk']} | "
            f"{', '.join(item['candidate_missing']) or '-'} | "
            f"{'改进' if item['better'] else '退化'} |"
        )
    return "\n".join(lines) + "\n"
