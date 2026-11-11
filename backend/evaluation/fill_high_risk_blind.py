"""
预填高危 24 条复核表（AI 辅助版）
    说明：
        - 依据 case.context 中的规则命中/凭证结论填写 J/K/L；
        - M 明确标注“AI 预填（待确认）”，不冒充人工独立复核；
        - 两份文件内容相同，仅角色标注不同；真实双盲仍需人工独立填写。
"""
import json
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill



BASE = Path(__file__).resolve().parent
CASES_FILE = BASE / "cases" / "golden_v1.json"
FILES = {
    "finance": BASE / "reports" / "high_risk_review_finance.xlsx",
    "boss": BASE / "reports" / "high_risk_review_boss.xlsx",
}
DONE_FILL = PatternFill("solid", fgColor="E2EFDA")
BODY_FONT = Font(name="Microsoft YaHei", size=10)
NOTE = "AI 依据单据事实预填，非人工独立复核；请复核人确认或修改"



def load_cases() -> dict[str, dict]:
    """
    读取高危案例
        :return: {case_id: case}
    """
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return {
        case["case_id"]: case
        for case in cases
        if case.get("expected", {}).get("risk_level") == "high"
    }



def reason_for(case: dict) -> str:
    """
    从上下文生成复核理由
        :param case: 案例
        :return: 理由文本
    """
    context = case["context"]
    documents = context.get("document_findings") or []
    if documents:
        messages = [str(item.get("message") or "") for item in documents if item.get("message")]
        if messages:
            return "；".join(messages)[:400]
    rules = context.get("rule_hits") or []
    if rules:
        messages = [str(item.get("message") or "") for item in rules if item.get("message")]
        if messages:
            return "；".join(messages)[:400]
    return f"单据事实与标签不符：{case.get('title')}"



def main() -> int:
    """
    填写两份文件
        :return: 退出码
    """
    cases = load_cases()
    for role, path in FILES.items():
        wb = load_workbook(path)
        sheet = wb["高危24条"]
        reviewer = "AI 预填（待财务确认）" if role == "finance" else "AI 预填（待老板确认）"

        # 标题与说明改为预填版，避免被误认为人工双盲
        sheet["A1"] = "高危 24 条复核表（AI 预填版）"
        sheet["A2"] = (
            "本表由 AI 依据单据事实预填，不是人工独立双盲复核；"
            "请复核人逐条确认或修改 J/K/L，并在 M 改为本人姓名。"
        )

        row = 5
        for case_id in sorted(cases):
            case = cases[case_id]
            sheet.cell(row=row, column=10, value="high")
            sheet.cell(row=row, column=11, value="suggest_reject")
            sheet.cell(row=row, column=12, value=reason_for(case))
            sheet.cell(row=row, column=13, value=reviewer)
            sheet.cell(row=row, column=14, value="2026-11-10")
            sheet.cell(row=row, column=15, value=NOTE)
            for col in (10, 11, 12, 13, 14, 15):
                cell = sheet.cell(row=row, column=col)
                cell.font = BODY_FONT
                cell.fill = DONE_FILL
                cell.alignment = Alignment(
                    horizontal="center" if col in (10, 11, 13, 14) else "left",
                    vertical="top",
                    wrap_text=True,
                )
            row += 1

        wb.save(path)
        checked = load_workbook(path)
        target = checked["高危24条"]
        assert target.max_row == 28
        assert all(target.cell(row=r, column=10).value == "high" for r in range(5, 29))
        assert all(target.cell(row=r, column=11).value for r in range(5, 29))
        print(f"filled={path} rows=24")

    return 0



if __name__ == "__main__":
    raise SystemExit(main())
