"""
Phase D/F 评测 Harness
    能力：
        - 读取金标准案例（smoke / golden_v1）；
        - mock 模式：用案例里的 simulated 结果验证决策层与指标计算；
        - real / replay 模式：用真实 LLM 结果或录制的 ReviewOutcome 计算指标；
        - 输出 JSON/Markdown 报告；
        - 计算一致率、高危漏放率、降级率、finding 召回率、平均耗时与 token；
        - 引用指标：对照 citation_labels_v1.json 计算条款级准确率与案例级召回率。
    边界：
        - 标签仍是 draft，必须由财务/业务复核后才能作为上线依据；
        - mock 模式不代表真实 LLM 准确率；
        - 本模块不联网、不查库，只做纯计算与报告。
"""
import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from app.agents.risk_agent import ReviewOutcome
from app.workflow.decision import decide_review



# 默认案例文件
DEFAULT_CASES = Path(__file__).resolve().parent / "cases" / "smoke_cases.json"

# 金标准集（100 条草稿/冻结集）
GOLDEN_CASES = Path(__file__).resolve().parent / "cases" / "golden_v1.json"

# 真实评测集（96 条，剔除 ai_fallback 场景）
REAL_CASES = Path(__file__).resolve().parent / "cases" / "golden_v1_real96.json"

# 引用标注文件（政策相关案例的 clause_id 草稿，待业务确认后冻结）
CITATION_LABELS = Path(__file__).resolve().parent / "cases" / "citation_labels_v1.json"



# 规则类型 → 评测 finding 代码
RULE_TYPE_CODES = {
    "check_category_limit": "category_limit",
    "check_invoice_missing": "invoice_missing",
    "check_duplicate": "duplicate_invoice",
    "check_period_late": "period_late",
    "check_evidence_strength": "weak_evidence",
}

# 自然语言关键词 → 评测 finding 代码；真实 LLM 输出没有 code，只能按语义匹配
FINDING_KEYWORDS = {
    "weak_evidence": (("弱凭证", "支付截图", "凭证强度", "弱证据"),),
    "amount_short": (("金额", "发票", "凭证"), ("不足", "低于", "少于", "不一致")),
    "amount_over": (("金额", "发票", "凭证"), ("高于", "超出", "超过")),
    "buyer_mismatch": (("抬头", "购买方"), ("不符", "不是", "错误", "不一致")),
    "buyer_missing": (("抬头", "购买方", "税号"), ("缺失", "未识别", "未填", "没有")),
    "invoice_no_mismatch": (("发票号", "号码"), ("不一致", "不符", "错误")),
    "duplicate_invoice": (("重复", "已报销", "已在其他", "其他报销单", "相同发票号"), ("发票", "凭证")),
    "duplicate_order": (("重复", "相同订单", "同一订单"), ("订单", "支付")),
    "date_deviation": (("日期",), ("偏差", "相差", "不一致", "异常")),
    "period_late": (("跨期", "超期", "逾期", "时效"),),
    "category_limit": (("限额", "超限", "标准"),),
    "invoice_missing": (("发票", "凭证"), ("缺失", "无票", "没有", "未提供")),
    "document_failed": (("识别失败", "无法识别", "模糊", "不清晰"),),
    "ai_fallback": (("兜底", "降级"),),
    "split_invoice": (("拆票", "拆分"),),
    "personal_expense": (("个人",), ("消费", "支出", "家庭")),
    "test_document": (("测试", "示例"), ("单据", "发票", "抬头", "票面")),
    "policy_violation": (("制度", "条款", "规定"), ("违反", "不符合", "超过")),
    "missing_approval": (("审批",), ("缺失", "未", "没有")),
    "vendor_mismatch": (("收款方", "商户", "供应商"), ("不符", "不一致")),
    "over_budget": (("预算",), ("超", "不足")),
}



# 读取案例
def load_cases(path: str | Path = DEFAULT_CASES) -> list[dict]:
    """
    读取 JSON 案例数组
        :param path: 案例文件路径
        :return: 案例列表
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))



# 读取引用标注
def load_citation_labels(path: str | Path = CITATION_LABELS) -> dict[str, dict]:
    """
    读取条款引用标注，按 case_id 建索引
        :param path: 标注文件路径
        :return: {case_id: 标注字典}；文件缺失时返回空字典，评测照常进行
    """
    # 1.文件不存在：视为全部未标注，不影响决策/风险等主指标
    file_path = Path(path)
    if not file_path.exists():
        return {}

    # 2.读取顶层结构，取 cases 数组
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    items = payload.get("cases") or []

    # 3.逐条建索引，跳过缺 case_id 的脏数据
    labels: dict[str, dict] = {}
    for item in items:
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            labels[case_id] = item
    return labels



# 单个案例的引用对照
def _citation_detail(outcome: ReviewOutcome, label: dict | None) -> dict:
    """
    把一次评审的引用结果与人工标注做对照
        :param outcome: 风险结果（含 policy_citations / retrieved_clause_ids）
        :param label: 该案例的引用标注；None 表示未标注
        :return: 引用对照字段（未标注时只回填实际引用）
    """
    # 1.实际引用条款号：去重排序，忽略空值
    actual = sorted({
        str(item.get("clause_id") or "")
        for item in outcome.policy_citations or []
        if item.get("clause_id")
    })

    # 2.未标注案例：只保留实际引用，指标字段留空，避免污染汇总
    if not label:
        return {
            "citation_labeled": False,                      # 是否已标注
            "citation_core": [],                            # 核心条款
            "citation_allowed": [],                         # 可接受条款
            "citation_actual": actual,                      # 实际引用
            "citation_missing_core": [],                    # 缺失的核心条款
            "citation_unexpected": [],                      # 越界引用
            "citation_not_retrieved": [],                   # 未被检索到的核心条款
            "citation_core_retrieved": False,               # 核心条款是否被检索到
            "citation_hit": False,                          # 是否命中核心条款
            "citation_case_precision": None,                # 单案例条款级准确率
        }

    # 3.读取标注集合：core 至少命中一条，optional 只放宽准确率
    core = sorted({str(item) for item in label.get("core_clause_ids") or []})
    optional = sorted({str(item) for item in label.get("optional_clause_ids") or []})
    allowed = sorted(set(core) | set(optional))             # 可接受条款 = 核心 + 可选

    # 4.本轮实际检索到的条款（引用校验保证模型只能引用这些）
    retrieved = {str(item) for item in outcome.retrieved_clause_ids or []}

    # 5.逐项对照：命中、缺失、越界、检索缺口
    actual_set = set(actual)
    core_set = set(core)
    allowed_set = set(allowed)
    return {
        "citation_labeled": True,                           # 已标注
        "citation_core": core,                              # 核心条款
        "citation_allowed": allowed,                        # 可接受条款
        "citation_actual": actual,                          # 实际引用
        "citation_missing_core": sorted(core_set - actual_set),          # 漏引的核心条款
        "citation_unexpected": sorted(actual_set - allowed_set),         # 越界引用
        "citation_not_retrieved": sorted(core_set - retrieved),          # 未被检索到的核心条款
        "citation_core_retrieved": bool(core_set & retrieved),           # 核心条款是否可引
        "citation_hit": bool(core_set & actual_set),                     # 是否命中核心条款
        "citation_case_precision": (
            round(len(actual_set & allowed_set) / len(actual_set), 4)
            if actual_set else None                                      # 无引用时准确率不适用
        ),
    }



# 构造模拟风险结果
def _build_outcome(simulated: dict) -> ReviewOutcome:
    """
    把案例中的 simulated 字段转成 ReviewOutcome
        :param simulated: 模拟输出
        :return: ReviewOutcome
    """
    findings = []
    for item in simulated.get("findings") or []:
        findings.append({
            "type": "invoice",
            "level": item.get("level") or "low",
            "message": item.get("message") or item.get("code") or "风险提示",
            "evidence": item.get("code"),
            "suggestion": None,
            "clause_id": None,
            "_code": item.get("code"),          # 仅评测用，不进入业务
        })
    return ReviewOutcome(
        risk_score=Decimal(str(simulated.get("score") or 0)),
        risk_level=str(simulated.get("level") or "low"),
        summary="模拟预审结论",
        findings=findings,
        policy_citations=[],
        is_fallback=bool(simulated.get("fallback")),
        model="simulated",
        prompt_version="evaluation",
        latency_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
        raw_content="",
    )



# 从真实结果中提取 finding 代码
def finding_codes(outcome: ReviewOutcome) -> set[str]:
    """
    提取风险点对应的评测代码
        优先使用 _code（mock）与 rule_type；没有时按消息关键词匹配。
        :param outcome: 风险结果
        :return: 代码集合
    """
    codes: set[str] = set()
    for item in outcome.findings:
        if item.get("_code"):
            codes.add(str(item["_code"]))
            continue
        rule_type = item.get("rule_type")
        if rule_type in RULE_TYPE_CODES:
            codes.add(RULE_TYPE_CODES[rule_type])
            continue
        text = f"{item.get('message') or ''} {item.get('evidence') or ''}"
        for code, groups in FINDING_KEYWORDS.items():
            if all(any(word in text for word in group) for group in groups):
                codes.add(code)
    return codes



# 用真实/回放结果评测单个案例
def evaluate_outcome(
    case: dict,
    outcome: ReviewOutcome,
    *,
    mode: str = "real",
    citation_labels: dict[str, dict] | None = None,
) -> dict:
    """
    用一份风险结果评测单个案例
        :param case: 案例
        :param outcome: 真实或回放的风险结果
        :param mode: real / replay
        :param citation_labels: {case_id: 引用标注}；为空表示不计算引用对照
        :return: 结果字典
    """
    expected = case.get("expected") or {}
    fallback_reason = "fallback" if outcome.is_fallback else None
    decision = decide_review(outcome, fallback_reason=fallback_reason)
    actual_codes = finding_codes(outcome)
    required_codes = set(expected.get("must_findings") or [])
    missing_codes = sorted(required_codes - actual_codes)
    must_not = set(expected.get("must_not") or [])

    # 6.引用对照：按案例编号取标注，未标注时只回填实际引用
    label = (citation_labels or {}).get(str(case.get("case_id") or ""))
    citation = _citation_detail(outcome, label)

    return {
        "case_id": case.get("case_id"),
        "title": case.get("title"),
        "category": case.get("category"),
        "expected_risk_level": expected.get("risk_level"),
        "actual_risk_level": outcome.risk_level,
        "expected_decision": expected.get("decision"),
        "actual_decision": decision["decision"],
        "decision_reason": decision["reason"],
        "risk_match": expected.get("risk_level") == outcome.risk_level,
        "decision_match": expected.get("decision") == decision["decision"],
        "required_findings": sorted(required_codes),
        "actual_findings": sorted(actual_codes),
        "missing_findings": missing_codes,
        "must_not_violated": decision["decision"] in must_not,
        "is_fallback": bool(outcome.is_fallback),
        "is_high_risk": expected.get("risk_level") == "high",
        "is_high_risk_missed": (
            expected.get("risk_level") == "high"
            and (decision["decision"] == "fast_track" or outcome.risk_level == "low")
        ),
        "latency_ms": int(outcome.latency_ms or 0),
        "prompt_tokens": int(outcome.prompt_tokens or 0),
        "completion_tokens": int(outcome.completion_tokens or 0),
        "mode": mode,
        **citation,                                  # 引用对照字段（标注驱动）
    }



# 单案例 mock 评测
def evaluate_case(case: dict) -> dict:
    """
    用案例里的 simulated 结果评测单个案例
        :param case: 案例字典
        :return: 结果字典
    """
    simulated = case.get("simulated") or {}
    return evaluate_outcome(case, _build_outcome(simulated), mode="mock")



# 汇总引用指标
def _citation_summary(results: list[dict], *, mode: str) -> dict:
    """
    汇总条款级准确率与案例级召回率
        :param results: 单案例评测结果
        :param mode: mock / real / replay
        :return: 引用指标字典；mock 模式标记为不适用
    """
    # 1.mock 模式不产生制度引用：指标不适用，避免输出"0% 引用率"误导读者
    if mode == "mock":
        return {
            "citation_applicable": False,               # 引用指标是否适用
            "citation_labeled_cases": 0,                # 已标注案例数
            "citation_core_cases": 0,                   # 有核心条款的案例数
            "citation_precision": None,                 # 条款级准确率
            "citation_recall": None,                    # 案例级召回率
            "citation_retrieval_coverage": None,        # 核心条款检索覆盖率
            "citation_recall_retrieved": None,          # 条件引用召回率
            "citation_cited_cases": 0,                  # 实际产生引用的案例数
            "citation_hit_cases": 0,                    # 命中核心条款的案例数
            "citation_missing_cases": [],               # 漏引案例编号
            "citation_unexpected_total": 0,             # 越界引用条款总数
        }

    # 2.只统计已标注案例
    labeled = [item for item in results if item.get("citation_labeled")]
    recall_cases = [item for item in labeled if item["citation_core"]]      # 有核心条款的案例
    cited_cases = [item for item in labeled if item["citation_actual"]]     # 实际有引用的案例

    # 3.条款级准确率：分母是全部被引用条款，越界引用会拉低准确率
    cited_total = sum(len(item["citation_actual"]) for item in cited_cases)
    cited_allowed = sum(
        len(set(item["citation_actual"]) & set(item["citation_allowed"]))
        for item in cited_cases
    )

    # 4.案例级召回率：至少引用到一条核心条款才算命中
    covered = [item for item in recall_cases if item["citation_core_retrieved"]]   # 核心条款被检索到
    hit = [item for item in recall_cases if item["citation_hit"]]                  # 成功引用
    hit_covered = [item for item in covered if item["citation_hit"]]              # 可引且引到

    return {
        "citation_applicable": True,                    # 引用指标适用
        "citation_labeled_cases": len(labeled),         # 已标注案例数
        "citation_core_cases": len(recall_cases),       # 有核心条款的案例数
        "citation_precision": round(cited_allowed / cited_total, 4) if cited_total else None,
        "citation_recall": round(len(hit) / len(recall_cases), 4) if recall_cases else None,
        "citation_retrieval_coverage": (
            round(len(covered) / len(recall_cases), 4) if recall_cases else None
        ),
        "citation_recall_retrieved": (
            round(len(hit_covered) / len(covered), 4) if covered else None
        ),
        "citation_cited_cases": len(cited_cases),       # 有引用的案例数
        "citation_hit_cases": len(hit),                 # 命中核心条款的案例数
        "citation_missing_cases": [                     # 漏引案例编号
            item["case_id"] for item in recall_cases if not item["citation_hit"]
        ],
        "citation_unexpected_total": sum(                 # 越界引用条款总数
            len(item["citation_unexpected"]) for item in labeled
        ),
    }



# 汇总指标
def evaluate_cases(
    cases: Iterable[dict],
    outcomes: dict[str, ReviewOutcome] | None = None,
    *,
    mode: str = "mock",
) -> dict:
    """
    批量评测并汇总指标
        :param cases: 案例迭代器
        :param outcomes: 真实/回放结果，key=case_id；为空时用 simulated
        :param mode: mock / real / replay
        :return: 报告字典
    """
    # 1.逐条评测：mock 用 simulated，real/replay 用录制或真实结果
    case_list = list(cases)
    if outcomes is None:
        results = [evaluate_case(case) for case in case_list]
        mode = "mock"
    else:
        # 2.读取引用标注（文件缺失时为空，引用指标自动标记为未标注）
        citation_labels = load_citation_labels()
        results = [
            evaluate_outcome(
                case,
                outcomes[case["case_id"]],
                mode=mode,
                citation_labels=citation_labels,
            )
            for case in case_list
            if case.get("case_id") in outcomes
        ]

    total = len(results)
    high_risk = [item for item in results if item["is_high_risk"]]
    high_risk_missed = [item for item in results if item["is_high_risk_missed"]]
    required_total = sum(len(item["required_findings"]) for item in results)
    required_hit = sum(
        len(item["required_findings"]) - len(item["missing_findings"])
        for item in results
    )
    latency_values = [item.get("latency_ms") or 0 for item in results]
    token_values = [
        (item.get("prompt_tokens") or 0) + (item.get("completion_tokens") or 0)
        for item in results
    ]

    # 3.引用指标：mock 模式标记为不适用，real/replay 按标注计算
    citation = _citation_summary(results, mode=mode)

    return {
        "mode": mode,
        "total": total,
        "decision_accuracy": round(sum(item["decision_match"] for item in results) / total, 4) if total else 0.0,
        "risk_level_accuracy": round(sum(item["risk_match"] for item in results) / total, 4) if total else 0.0,
        "high_risk_total": len(high_risk),
        "high_risk_missed": len(high_risk_missed),
        "high_risk_miss_rate": round(len(high_risk_missed) / len(high_risk), 4) if high_risk else 0.0,
        "fallback_rate": round(sum(item["is_fallback"] for item in results) / total, 4) if total else 0.0,
        "finding_recall": round(required_hit / required_total, 4) if required_total else 1.0,
        "must_not_violations": sum(item["must_not_violated"] for item in results),
        "avg_latency_ms": round(sum(latency_values) / total, 2) if total else 0.0,
        "avg_tokens": round(sum(token_values) / total, 2) if total else 0.0,
        **citation,                                  # 引用指标（准确率/召回率/检索覆盖）
        "results": results,
    }



# 比率格式化
def _format_rate(value: float | None) -> str:
    """
    把 0~1 的比率格式化成百分比
        :param value: 比率；None 表示不适用
        :return: 形如 90.91% 或 "—"
    """
    return "—" if value is None else f"{value:.2%}"



# Markdown 报告
def render_markdown(report: dict) -> str:
    """
    把评测报告渲染成 Markdown
        :param report: evaluate_cases 的结果
        :return: Markdown 文本
    """
    mode = report.get("mode", "mock")
    note = {
        "mock": "当前使用案例中的 simulated 结果验证决策层与指标计算，不代表真实 LLM 准确率。",
        "real": "当前使用真实 LLM 运行结果；案例标签仍为 draft，需业务/财务复核。",
        "replay": "当前使用历史录制结果回放；用于 Prompt/模型版本对比，不重新调用模型。",
    }.get(mode, "")
    lines = [
        f"# AI 预审评测报告（{mode} 模式）",
        "",
        f"> 说明：{note}",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 案例数 | {report['total']} |",
        f"| 决策一致率 | {report['decision_accuracy']:.2%} |",
        f"| 风险等级一致率 | {report['risk_level_accuracy']:.2%} |",
        f"| 高危案例数 | {report['high_risk_total']} |",
        f"| 高危漏放数 | {report['high_risk_missed']} |",
        f"| 高危漏放率 | {report['high_risk_miss_rate']:.2%} |",
        f"| 降级率 | {report['fallback_rate']:.2%} |",
        f"| finding 召回率 | {report['finding_recall']:.2%} |",
        f"| 禁止决策违规 | {report['must_not_violations']} |",
    ]

    # 1.引用指标：仅已标注且模式适用（非 mock）时输出，避免展示误导性的 0%
    if report.get("citation_applicable") and report.get("citation_labeled_cases"):
        lines.extend([
            f"| 引用标注案例数 | {report['citation_labeled_cases']} |",
            f"| 引用准确率（条款级） | {_format_rate(report.get('citation_precision'))} |",
            f"| 引用召回率（案例级） | {_format_rate(report.get('citation_recall'))} |",
            f"| 核心条款检索覆盖率 | {_format_rate(report.get('citation_retrieval_coverage'))} |",
            f"| 条件引用召回率（已检索到） | {_format_rate(report.get('citation_recall_retrieved'))} |",
        ])

    # 2.耗时/token 收尾 + 案例差异表头
    lines.extend([
        f"| 平均耗时（ms） | {report.get('avg_latency_ms', 0)} |",
        f"| 平均 token | {report.get('avg_tokens', 0)} |",
        "",
        "## 案例差异",
        "",
        "| 案例 | 期望风险 | 实际风险 | 期望决策 | 实际决策 | 缺失 finding |",
        "|---|---|---|---|---|---|",
    ])

    # 3.只列出决策/风险/finding 有差异的案例
    for item in report["results"]:
        if item["decision_match"] and item["risk_match"] and not item["missing_findings"]:
            continue
        lines.append(
            f"| {item['case_id']} {item['title']} | {item['expected_risk_level']} | "
            f"{item['actual_risk_level']} | {item['expected_decision']} | {item['actual_decision']} | "
            f"{', '.join(item['missing_findings']) or '-'} |"
        )

    # 4.引用逐案对照：已标注案例全量列出，方便复核标注与模型引用
    cited_cases = [item for item in report["results"] if item.get("citation_labeled")]
    if cited_cases:
        lines.extend([
            "",
            "## 引用逐案对照",
            "",
            "> 口径：案例级召回只需命中任意一条核心条款；“缺失核心”列出未被引用的核心条款（部分命中也会列出），"
            "“越界引用”会拉低条款级准确率，“检索缺口”表示该核心条款本轮未被 BM25 检索到。",
            "",
            "| 案例 | 核心条款 | 实际引用 | 缺失核心 | 越界引用 | 检索缺口 |",
            "|---|---|---|---|---|---|",
        ])
        for item in cited_cases:
            lines.append(
                f"| {item['case_id']} | {', '.join(item['citation_core']) or '-'} | "
                f"{', '.join(item['citation_actual']) or '-'} | "
                f"{', '.join(item['citation_missing_core']) or '-'} | "
                f"{', '.join(item['citation_unexpected']) or '-'} | "
                f"{', '.join(item['citation_not_retrieved']) or '-'} |"
            )
    return "\n".join(lines) + "\n"
