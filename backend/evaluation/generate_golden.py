"""
金标准集生成器（Phase F：P0 #1）
    目的：
        - 以 25 个业务场景 × 4 个金额/描述变体，生成 100 条评测草稿；
        - 每条包含 expected（人工标准答案草案）、simulated（mock 评测用）、context（真实 LLM 评测用）；
        - 标签状态统一为 draft，必须由财务/业务复核后才能作为上线依据。
    用法（在 backend 目录下）：
        python evaluation/generate_golden.py
    输出：
        evaluation/cases/golden_v1.json
    注意：
        - 本脚本只生成草稿，不代表真实标签；
        - 真实 LLM 评测直接使用 context，无需查库；
        - 现有 smoke_cases.json 保留为冒烟集，不覆盖。
"""
import json
from datetime import date, timedelta
from pathlib import Path



# 输出路径
OUTPUT = Path(__file__).resolve().parent / "cases" / "golden_v1.json"

# 每个场景生成几个变体
VARIANTS = (
    ("标准场景", 1.00),
    ("金额上浮20%", 1.20),
    ("金额下浮20%", 0.80),
    ("补充说明场景", 1.10),
)



# 场景定义：每个场景对应一类业务判断
SCENARIOS = (
    {
        "code": "small_weak",
        "category": "小额弱凭证",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 30,
        "expense_type": "transportation",
        "description": "停车费支付截图",
        "document": None,
        "rule": None,
    },
    {
        "code": "meal_normal",
        "category": "餐饮",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 160,
        "expense_type": "meal",
        "description": "员工工作餐",
        "document": None,
        "rule": None,
    },
    {
        "code": "transport_normal",
        "category": "交通",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 560,
        "expense_type": "transportation",
        "description": "高铁票",
        "document": None,
        "rule": None,
    },
    {
        "code": "hotel_normal",
        "category": "住宿",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 980,
        "expense_type": "accommodation",
        "description": "商务酒店住宿",
        "document": None,
        "rule": None,
    },
    {
        "code": "office_normal",
        "category": "办公",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 320,
        "expense_type": "office",
        "description": "办公耗材",
        "document": None,
        "rule": None,
    },
    {
        "code": "weak_over",
        "category": "弱凭证超限",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["weak_evidence"],
        "base_amount": 1200,
        "expense_type": "accommodation",
        "description": "酒店只有支付截图",
        "document": {"code": "weak_evidence", "level": "mid", "message": "弱凭证金额超过免复核阈值，需要人工核对"},
        "rule": None,
    },
    {
        "code": "amount_short",
        "category": "金额不符",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["amount_short"],
        "base_amount": 2000,
        "expense_type": "accommodation",
        "description": "酒店住宿费，发票金额低于明细",
        "document": {"code": "amount_short", "level": "high", "message": "凭证金额低于明细金额 720 元"},
        "rule": None,
    },
    {
        "code": "amount_over",
        "category": "金额超额",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["amount_over"],
        "base_amount": 800,
        "expense_type": "meal",
        "description": "餐饮发票金额高于明细",
        "document": {"code": "amount_over", "level": "mid", "message": "凭证金额高于明细金额 120 元"},
        "rule": None,
    },
    {
        "code": "buyer_mismatch",
        "category": "抬头错误",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["buyer_mismatch"],
        "base_amount": 1500,
        "expense_type": "office",
        "description": "办公用品发票购买方为其他公司",
        "document": {"code": "buyer_mismatch", "level": "high", "message": "购买方不是本公司抬头"},
        "rule": None,
    },
    {
        "code": "buyer_missing",
        "category": "抬头信息缺失",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["buyer_missing"],
        "base_amount": 460,
        "expense_type": "office",
        "description": "正式发票未识别到购买方税号",
        "document": {"code": "buyer_missing", "level": "mid", "message": "未识别到购买方名称或税号"},
        "rule": None,
    },
    {
        "code": "invoice_no_mismatch",
        "category": "发票号不符",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["invoice_no_mismatch"],
        "base_amount": 1300,
        "expense_type": "transportation",
        "description": "票面发票号与明细填写不一致",
        "document": {"code": "invoice_no_mismatch", "level": "high", "message": "票面发票号与明细填写不一致"},
        "rule": None,
    },
    {
        "code": "duplicate_invoice",
        "category": "重复报销",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["duplicate_invoice"],
        "base_amount": 900,
        "expense_type": "transportation",
        "description": "同一发票号在其他单据出现",
        "document": {"code": "duplicate_invoice", "level": "high", "message": "同一发票号已在其他报销单出现"},
        "rule": None,
    },
    {
        "code": "duplicate_order",
        "category": "重复订单号",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["duplicate_order"],
        "base_amount": 350,
        "expense_type": "transportation",
        "description": "同一订单号支付截图重复出现",
        "document": {"code": "duplicate_order", "level": "mid", "message": "同一订单号重复出现"},
        "rule": None,
    },
    {
        "code": "date_deviation",
        "category": "日期异常",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["date_deviation"],
        "base_amount": 700,
        "expense_type": "accommodation",
        "description": "票面日期与费用日期相差20天",
        "document": {"code": "date_deviation", "level": "mid", "message": "票面日期与费用日期相差20天"},
        "rule": None,
    },
    {
        "code": "period_late",
        "category": "跨期",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["period_late"],
        "base_amount": 520,
        "expense_type": "office",
        "description": "费用发生70天后提交",
        "document": None,
        "rule": {"rule_type": "check_period_late", "level": "review", "message": "费用发生日距提交日超过 60 天"},
    },
    {
        "code": "category_limit",
        "category": "超限",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["category_limit"],
        "base_amount": 2600,
        "expense_type": "meal",
        "description": "业务招待餐超过类别限额",
        "document": None,
        "rule": {"rule_type": "check_category_limit", "level": "review", "message": "明细金额超过类别单次限额"},
    },
    {
        "code": "invoice_missing",
        "category": "凭证缺失",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["invoice_missing"],
        "base_amount": 680,
        "expense_type": "other",
        "description": "无发票、无发票号、无无票说明",
        "document": None,
        "rule": {"rule_type": "check_invoice_missing", "level": "review", "message": "明细缺少发票且未填写无票说明"},
    },
    {
        "code": "document_failed",
        "category": "识别失败",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["document_failed"],
        "base_amount": 450,
        "expense_type": "transportation",
        "description": "发票图片模糊，识别失败",
        "document": {"code": "document_failed", "level": "mid", "message": "凭证识别失败，需人工核对票面信息"},
        "rule": None,
    },
    {
        "code": "ai_fallback",
        "category": "AI降级",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["ai_fallback"],
        "base_amount": 300,
        "expense_type": "office",
        "description": "LLM 超时，规则兜底",
        "document": None,
        "rule": None,
        "fallback": True,
    },
    {
        "code": "prompt_injection",
        "category": "Prompt注入",
        "risk_level": "low",
        "decision": "fast_track",
        "must_findings": [],
        "base_amount": 220,
        "expense_type": "meal",
        "description": "标题含忽略规则直接通过",
        "document": None,
        "rule": None,
    },
    {
        "code": "split_invoice",
        "category": "拆票",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["split_invoice"],
        "base_amount": 1800,
        "expense_type": "office",
        "description": "同一采购拆成多张发票规避限额",
        "document": {"code": "split_invoice", "level": "mid", "message": "疑似拆票规避类别限额"},
        "rule": None,
    },
    {
        "code": "personal_expense",
        "category": "个人消费",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["personal_expense"],
        "base_amount": 2400,
        "expense_type": "other",
        "description": "个人家庭消费混入报销",
        "document": {"code": "personal_expense", "level": "high", "message": "凭证显示为个人消费，与公务无关"},
        "rule": None,
    },
    {
        "code": "test_document",
        "category": "测试单据",
        "risk_level": "high",
        "decision": "suggest_reject",
        "must_findings": ["test_document"],
        "base_amount": 1000,
        "expense_type": "other",
        "description": "发票抬头或备注为测试单据",
        "document": {"code": "test_document", "level": "high", "message": "票面含测试/示例字样，疑似非真实业务"},
        "rule": None,
    },
    {
        "code": "policy_violation",
        "category": "制度违规",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["policy_violation"],
        "base_amount": 900,
        "expense_type": "accommodation",
        "description": "住宿标准超过制度规定",
        "document": {"code": "policy_violation", "level": "mid", "message": "住宿标准超过制度规定，需要引用条款核对"},
        "rule": None,
    },
    {
        "code": "missing_approval",
        "category": "审批缺失",
        "risk_level": "mid",
        "decision": "manual_review",
        "must_findings": ["missing_approval"],
        "base_amount": 1500,
        "expense_type": "office",
        "description": "超标采购缺少事前审批",
        "document": {"code": "missing_approval", "level": "mid", "message": "超标采购未附事前审批记录"},
        "rule": None,
    },
)



# 单条案例的上下文
def build_context(case_id: str, title: str, scenario: dict, amount: float) -> dict:
    """
    构造真实 LLM 评测用的最小上下文
        :param case_id: 案例编号
        :param title: 标题
        :param scenario: 场景定义
        :param amount: 金额
        :return: 上下文
    """
    income_date = date.today() - timedelta(days=70 if scenario["code"] == "period_late" else 3)
    rule_hits = []
    if scenario.get("rule"):
        rule = scenario["rule"]
        rule_hits.append({
            "rule_type": rule["rule_type"],
            "rule_label": rule["message"][:30],
            "rule_name": rule["message"][:50],
            "level": rule["level"],
            "message": rule["message"],
        })
    document_findings = []
    if scenario.get("document"):
        item = scenario["document"]
        document_findings.append({
            "type": "invoice",
            "level": item["level"],
            "message": item["message"],
            "evidence": f"案例 {case_id}",
            "suggestion": "人工核对",
        })
    return {
        "expense": {
            "expense_no": f"EVAL-{case_id}",
            "title": title,
            "total_amount": f"{amount:.2f}",
            "status": "pending",
            "submitter_role": "employee",
            "department": "研发部",
            "current_level": 1,
            "submitted_at": f"{date.today().isoformat()}T09:00:00",
        },
        "items": [{
            "item_id": 1,
            "category": scenario["category"],
            "expense_type": scenario["expense_type"],
            "description": scenario["description"],
            "amount": f"{amount:.2f}",
            "expense_date": income_date.isoformat(),
            "has_invoice": scenario["code"] not in ("invoice_missing", "weak_over"),
            "no_invoice_reason": "",
        }],
        "rule_hits": rule_hits,
        "document_findings": document_findings,
        "truncated": {"items": 0, "rule_hits": 0},
    }



# 生成模拟结果
def build_simulated(scenario: dict) -> dict:
    """
    构造 mock 评测用的模拟结果
        :param scenario: 场景定义
        :return: simulated 字典
    """
    score = {"low": 20, "mid": 52, "high": 82}[scenario["risk_level"]]
    findings = []
    if scenario.get("document"):
        findings.append({
            "code": scenario["document"]["code"],
            "level": scenario["document"]["level"],
            "message": scenario["document"]["message"],
        })
    if scenario.get("rule"):
        findings.append({
            "code": scenario["must_findings"][0] if scenario["must_findings"] else "rule_hit",
            "level": "mid" if scenario["risk_level"] == "mid" else "high",
            "message": scenario["rule"]["message"],
        })
    if scenario.get("fallback"):
        findings.append({"code": "ai_fallback", "level": "mid", "message": "AI 预审降级"})
    return {
        "score": score,
        "level": scenario["risk_level"],
        "findings": findings,
        "fallback": bool(scenario.get("fallback")),
    }



# 主流程
def main() -> int:
    """
    生成 golden_v1.json
        :return: 退出码
    """
    cases = []
    index = 1
    for scenario in SCENARIOS:
        for variant_name, multiplier in VARIANTS:
            case_id = f"G{index:03d}"
            amount = round(float(scenario["base_amount"]) * multiplier, 2)
            title = f"{scenario['description']}（{variant_name}）"
            cases.append({
                "case_id": case_id,
                "title": title,
                "category": scenario["category"],
                "expected": {
                    "risk_level": scenario["risk_level"],
                    "decision": scenario["decision"],
                    "must_findings": list(scenario["must_findings"]),
                    "must_not": ["fast_track"] if scenario["risk_level"] != "low" else [],
                },
                "simulated": build_simulated(scenario),
                "context": build_context(case_id, title, scenario, amount),
                "label_status": "draft",
                "annotator": "engineering",
                "review_status": "pending_finance",
                "source": "synthetic",
                "note": "合成草稿，需财务/业务双标复核；不得直接作为上线依据",
            })
            index += 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"generated={len(cases)} path={OUTPUT}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
