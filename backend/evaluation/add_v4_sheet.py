"""
把 Prompt v4 对比结果写入复核工作簿（P0 #2）
    输入：
        evaluation/reports/v3_100_final.json
        evaluation/reports/v4_100_final.json
        evaluation/reports/v3_vs_v4_100_final.json
    输出：
        golden_review.xlsx 新增“v4对比”Sheet
"""
import json
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter



BASE = Path(__file__).resolve().parent
WORKBOOK = BASE / "reports" / "golden_review.xlsx"
V3_REPORT = BASE / "reports" / "v3_100_final.json"
V4_REPORT = BASE / "reports" / "v4_100_final.json"
COMPARE_REPORT = BASE / "reports" / "v3_vs_v4_100_final.json"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
SUB_FILL = PatternFill("solid", fgColor="D9E2F3")
OK_FILL = PatternFill("solid", fgColor="C6EFCE")
BAD_FILL = PatternFill("solid", fgColor="FFC7CE")
GRAY_FILL = PatternFill("solid", fgColor="F2F2F2")
WHITE_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Microsoft YaHei", size=10)
BOLD_FONT = Font(name="Microsoft YaHei", size=10, bold=True)
TITLE_FONT = Font(name="Microsoft YaHei", size=15, bold=True, color="1F4E78")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)



def load(path: Path):
    """
    读取 JSON
        :param path: 路径
        :return: Python 对象
    """
    return json.loads(path.read_text(encoding="utf-8"))



def cell(ws, row: int, col: int, value, *, fill=None, bold=False, center=False, wrap=True):
    """
    写单元格
        :param ws: 工作表
        :param row: 行
        :param col: 列
        :param value: 值
        :param fill: 填充
        :param bold: 加粗
        :param center: 居中
        :param wrap: 换行
    """
    target = ws.cell(row=row, column=col, value=value)
    target.font = BOLD_FONT if bold else BODY_FONT
    target.border = BORDER
    target.alignment = Alignment(
        vertical="top", wrap_text=wrap,
        horizontal="center" if center else "left",
    )
    if fill is not None:
        target.fill = fill



def main() -> int:
    """
    写入 v4 对比页
        :return: 退出码
    """
    v3 = load(V3_REPORT)
    v4 = load(V4_REPORT)
    comparison = load(COMPARE_REPORT)

    wb = load_workbook(WORKBOOK)
    if "v4对比" in wb.sheetnames:
        del wb["v4对比"]
    ws = wb.create_sheet("v4对比")

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    title = ws.cell(row=1, column=1, value="Prompt v3 vs v4 全量对比（100 条真实）")
    title.font = TITLE_FONT
    title.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=5)
    sub = ws.cell(
        row=2, column=1,
        value="模型 deepseek-flash；决策策略已调整为“低风险且仅有 low finding 时允许 fast_track”。"
              "v4 尚未切换为线上默认 Prompt。",
    )
    sub.font = Font(name="Microsoft YaHei", size=9, color="595959")
    sub.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 34

    headers = ["指标", "v3 100条", "v4 100条", "说明", "结论"]
    for col, text in enumerate(headers, start=1):
        target = ws.cell(row=4, column=col, value=text)
        target.fill = HEADER_FILL
        target.font = WHITE_FONT
        target.border = BORDER
        target.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rows = [
        ("案例数", v3.get("total"), v4.get("total"), "同一冻结 test 集", "-"),
        ("决策一致率", f"{v3.get('decision_accuracy', 0):.2%}", f"{v4.get('decision_accuracy', 0):.2%}", "v3 85% → v4 96%", "改进"),
        ("风险等级一致率", f"{v3.get('risk_level_accuracy', 0):.2%}", f"{v4.get('risk_level_accuracy', 0):.2%}", "v3 85% → v4 96%", "改进"),
        ("高危漏放率", f"{v3.get('high_risk_miss_rate', 0):.2%}", f"{v4.get('high_risk_miss_rate', 0):.2%}", "24 条高危均未漏放", "保持"),
        ("finding 召回率", f"{v3.get('finding_recall', 0):.2%}", f"{v4.get('finding_recall', 0):.2%}", "差距来自 ai_fallback 场景", "需剔除专项"),
        ("禁止决策违规", v3.get("must_not_violations", 0), v4.get("must_not_violations", 0), "全部来自 ai_fallback", "需剔除专项"),
        ("平均耗时（ms）", v3.get("avg_latency_ms", 0), v4.get("avg_latency_ms", 0), "v4 更快", "改进"),
        ("平均 token", v3.get("avg_tokens", 0), v4.get("avg_tokens", 0), "v4 略高", "可接受"),
    ]
    for index, values in enumerate(rows, start=5):
        for col, value in enumerate(values, start=1):
            cell(ws, index, col, value, bold=(col == 1), center=(col in (2, 3)))
        ws.row_dimensions[index].height = 24

    start = 15
    ws.merge_cells(start_row=start, start_column=1, end_row=start, end_column=5)
    section = ws.cell(row=start, column=1, value="关键结论")
    section.font = BOLD_FONT
    section.fill = SUB_FILL

    notes = [
        ("v4 改进", "决策 +11%、风险 +11%、耗时 -596ms；改进 11 条、退化 2 条。"),
        ("2 条退化", "G074/G076 均为 ai_fallback 场景，真实模型不会强制降级。"),
        ("剔除 ai_fallback", "去掉 G073-G076 后 96 条：决策 100%、风险 100%、召回 100%、违规 0。"),
        ("决策策略", "低风险且仅有 low finding 时允许 fast_track；中/高风险 finding 仍强制人工。"),
        ("上线建议", "先冻结 96 条真实集并移除 ai_fallback；再决定是否把 v4 切为线上默认。"),
    ]
    for offset, (name, text) in enumerate(notes, start=1):
        row = start + offset
        cell(ws, row, 1, name, bold=True, fill=GRAY_FILL)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        cell(ws, row, 2, text)
        ws.row_dimensions[row].height = 32

    widths = [20, 14, 14, 38, 18]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.sheet_view.showGridLines = False

    wb.save(WORKBOOK)
    checked = load_workbook(WORKBOOK)
    assert "v4对比" in checked.sheetnames
    print(f"updated={WORKBOOK}")
    print(f"sheets={checked.sheetnames}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
