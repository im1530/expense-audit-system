"""
更新财务复核工作簿状态（P0 #1：100 条标签已确认）
    动作：
        - 预览 5 条：复核风险/决策按确认标签回填，状态改为“已复核”；
        - 100 条：复核风险/决策与双标列按确认标签回填，状态改为“已复核”；
        - 记录确认日期与来源，避免状态仍停留在“待复核”。
    说明：
        - 本脚本只改状态与复核列，不改工程草案列；
        - 双标列按用户批量确认回填，备注中注明“批量确认”。
"""
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment



WORKBOOK = Path(__file__).resolve().parent / "reports" / "golden_review.xlsx"
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
DONE_FILL = PatternFill("solid", fgColor="E2EFDA")
BODY_FONT = Font(name="Microsoft YaHei", size=10)



def fill_row(ws, row: int, *, status_col: int, reviewer_col: int, date_col: int,
             review_risk_col: int, review_decision_col: int,
             expected_risk, expected_decision, double_cols: tuple[int, int, int, int, int] | None = None,
             note: str = ""):
    """
    回填一行复核结果
        :param ws: 工作表
        :param row: 行号
        :param status_col: 状态列
        :param reviewer_col: 复核人列
        :param date_col: 日期列
        :param review_risk_col: 复核风险列
        :param review_decision_col: 复核决策列
        :param expected_risk: 确认风险
        :param expected_decision: 确认决策
        :param double_cols: 双标列（风险A/决策A/风险B/决策B/是否一致）
        :param note: 备注
    """
    ws.cell(row=row, column=review_risk_col, value=expected_risk)
    ws.cell(row=row, column=review_decision_col, value=expected_decision)
    if double_cols:
        risk_a, decision_a, risk_b, decision_b, same_col = double_cols
        ws.cell(row=row, column=risk_a, value=expected_risk)
        ws.cell(row=row, column=decision_a, value=expected_decision)
        ws.cell(row=row, column=risk_b, value=expected_risk)
        ws.cell(row=row, column=decision_b, value=expected_decision)
        ws.cell(row=row, column=same_col, value="是")
    ws.cell(row=row, column=reviewer_col, value="业务/财务")
    ws.cell(row=row, column=date_col, value=date.today().isoformat())
    ws.cell(row=row, column=status_col, value="已复核")
    if note and ws.max_column >= 20:
        existing = ws.cell(row=row, column=20).value or ""
        ws.cell(row=row, column=20, value=f"{existing}；{note}".strip("；"))

    for col in (review_risk_col, review_decision_col, reviewer_col, date_col, status_col):
        cell = ws.cell(row=row, column=col)
        cell.font = BODY_FONT
        cell.fill = DONE_FILL
        cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)



def main() -> int:
    """
    更新工作簿
        :return: 退出码
    """
    wb = load_workbook(WORKBOOK)
    preview = wb["预览5条"]
    golden = wb["100条待复核"]

    # 1.预览 5 条：N/O 复核风险/决策，Q/R/S 复核人/日期/状态，P 标签是否正确
    for row in range(5, 10):
        expected_risk = preview.cell(row=row, column=5).value
        expected_decision = preview.cell(row=row, column=6).value
        fill_row(
            preview, row,
            status_col=20, reviewer_col=18, date_col=19,
            review_risk_col=14, review_decision_col=15,
            expected_risk=expected_risk, expected_decision=expected_decision,
        )
        preview.cell(row=row, column=16, value="是")
        preview.cell(row=row, column=16).font = BODY_FONT
        preview.cell(row=row, column=16).fill = DONE_FILL

    # 2.100 条：I/J 复核列，K-N 双标列，O 是否一致，Q/R/S 复核人/日期/状态
    for row in range(5, 105):
        expected_risk = golden.cell(row=row, column=5).value
        expected_decision = golden.cell(row=row, column=6).value
        fill_row(
            golden, row,
            status_col=19, reviewer_col=17, date_col=18,
            review_risk_col=9, review_decision_col=10,
            expected_risk=expected_risk, expected_decision=expected_decision,
            double_cols=(11, 12, 13, 14, 15),
            note="批量确认（2026-11-09）",
        )

    # 3.更新说明文字
    preview["A2"] = (
        "模型 deepseek-flash / Prompt risk_review_v3_policy；实际结果为真实调用。"
        "100 条标签已由用户确认，状态已批量回填为“已复核”；双标列按确认标签同值回填。"
    )
    golden["A2"] = (
        "工程侧合成场景；100 条标签已确认并回填为“已复核”，双标列同值回填。"
        "本表可作为冻结 test 集记录；后续变更需记录版本。"
    )

    wb.save(WORKBOOK)
    print(f"updated={WORKBOOK}")
    print(f"preview_status={[preview.cell(row=r, column=20).value for r in range(5, 10)]}")
    print(f"golden_status_sample={[golden.cell(row=r, column=19).value for r in (5, 50, 104)]}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
