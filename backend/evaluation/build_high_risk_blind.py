"""
生成高危 24 条双盲复核表（P0 #1）
    复核人规则：
        A = 任意财务（finance）
        B = 老板（boss）
        备选：两位财务
    设计：
        - 两份独立文件，A/B 互不可见；
        - 不包含工程草案 expected、simulated 或模型结果，避免锚定；
        - 只展示单据事实、规则命中、凭证结论，由复核人独立判断。
"""
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
import json



BASE = Path(__file__).resolve().parent
CASES_FILE = BASE / "cases" / "golden_v1.json"
OUT_DIR = BASE / "reports"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
GRAY_FILL = PatternFill("solid", fgColor="F2F2F2")
WHITE_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Microsoft YaHei", size=10)
BOLD_FONT = Font(name="Microsoft YaHei", size=10, bold=True)
TITLE_FONT = Font(name="Microsoft YaHei", size=15, bold=True, color="1F4E78")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

HEADERS = [
    "案例编号", "标题", "分类", "金额", "费用说明", "费用日期",
    "是否有票", "规则命中", "凭证/票面结论",
    "复核风险", "复核决策", "复核理由", "复核人", "复核日期", "备注",
]



def load_cases() -> list[dict]:
    """
    读取 100 条案例并筛出高危 24 条
        :return: 高危案例
    """
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    return [case for case in cases if case.get("expected", {}).get("risk_level") == "high"]



def write_cell(ws, row: int, col: int, value, *, fill=None, center=False, bold=False, wrap=True):
    """
    写单元格
        :param ws: 工作表
        :param row: 行
        :param col: 列
        :param value: 值
        :param fill: 填充
        :param center: 居中
        :param bold: 加粗
        :param wrap: 换行
    """
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = BOLD_FONT if bold else BODY_FONT
    cell.border = BORDER
    cell.alignment = Alignment(
        vertical="top", wrap_text=wrap,
        horizontal="center" if center else "left",
    )
    if fill is not None:
        cell.fill = fill



def build_sheet(ws, cases: list[dict], role: str):
    """
    构建复核表
        :param ws: 工作表
        :param cases: 高危案例
        :param role: 复核人角色说明
    """
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    title = ws.cell(row=1, column=1, value=f"高危 24 条双盲复核表 — {role}")
    title.font = TITLE_FONT
    title.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(HEADERS))
    sub = ws.cell(
        row=2, column=1,
        value="请独立判断，不要参考工程草案、模型结果或其他复核人的意见。"
              "复核风险=low/mid/high；复核决策=fast_track/manual_review/suggest_reject。",
    )
    sub.font = Font(name="Microsoft YaHei", size=9, color="595959")
    sub.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 34

    for index, text in enumerate(HEADERS, start=1):
        cell = ws.cell(row=4, column=index, value=text)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 34

    for row_index, case in enumerate(cases, start=5):
        context = case["context"]
        expense = context["expense"]
        item = context["items"][0]
        rules = "\n".join(
            f"[{hit.get('level')}] {hit.get('message')}" for hit in context.get("rule_hits", [])
        ) or "无"
        documents = "\n".join(
            f"[{item.get('level')}] {item.get('message')}" for item in context.get("document_findings", [])
        ) or "无"
        values = [
            case["case_id"],
            case["title"],
            case["category"],
            expense.get("total_amount"),
            item.get("description"),
            item.get("expense_date"),
            "有" if item.get("has_invoice") else "无",
            rules,
            documents,
            None,   # 复核风险
            None,   # 复核决策
            None,   # 理由
            None,   # 复核人
            None,   # 日期
            None,   # 备注
        ]
        for col, value in enumerate(values, start=1):
            fill = REVIEW_FILL if 10 <= col <= 15 else (GRAY_FILL if col in (8, 9) else None)
            write_cell(
                ws, row_index, col, value, fill=fill,
                center=col in (1, 4, 7, 10, 11, 14),
            )
        ws.row_dimensions[row_index].height = 62

    widths = [10, 30, 14, 10, 28, 12, 9, 30, 42, 11, 18, 32, 14, 12, 24]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:O{4 + len(cases)}"
    ws.sheet_view.showGridLines = False

    risk_dv = DataValidation(type="list", formula1='"low,mid,high"', allow_blank=True)
    decision_dv = DataValidation(
        type="list",
        formula1='"fast_track,manual_review,suggest_reject"',
        allow_blank=True,
    )
    ws.add_data_validation(risk_dv)
    ws.add_data_validation(decision_dv)
    risk_dv.add(f"J5:J{4 + len(cases)}")
    decision_dv.add(f"K5:K{4 + len(cases)}")



def build_guide(ws, role: str):
    """
    构建填写说明页
        :param ws: 工作表
        :param role: 复核人角色
    """
    ws.merge_cells("A1:B1")
    title = ws.cell(row=1, column=1, value="填写说明")
    title.font = TITLE_FONT
    ws.row_dimensions[1].height = 28

    rows = [
        ("复核人", role),
        ("复核范围", "24 条高危案例（金额不符、抬头错误、发票号不符、重复报销、个人消费、测试单据）"),
        ("独立性", "不要查看工程草案、模型结果、另一复核人的表格；先独立判断再讨论"),
        ("复核风险", "low / mid / high，按制度与业务实质判断"),
        ("复核决策", "fast_track / manual_review / suggest_reject，只给建议，不自动审批"),
        ("复核理由", "写清楚依据，例如金额差、抬头不符、发票号重复、制度条款等"),
        ("差异处理", "A/B 不一致的案例由财务与老板讨论，形成最终标签"),
        ("注意", "本表不含工程标签，也不含模型结果；请只依据单据事实判断"),
    ]
    for index, (name, text) in enumerate(rows, start=3):
        ws.cell(row=index, column=1, value=name).font = BOLD_FONT
        ws.cell(row=index, column=1).fill = GRAY_FILL
        ws.cell(row=index, column=1).border = BORDER
        ws.cell(row=index, column=2, value=text).font = BODY_FONT
        ws.cell(row=index, column=2).border = BORDER
        ws.cell(row=index, column=2).alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[index].height = 28
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 86
    ws.sheet_view.showGridLines = False



def build(role: str, filename: str):
    """
    生成一份复核文件
        :param role: 角色说明
        :param filename: 输出文件名
    """
    cases = load_cases()
    wb = Workbook()
    wb.active.title = "高危24条"
    build_sheet(wb.active, cases, role)
    build_guide(wb.create_sheet("填写说明"), role)
    wb.properties.title = f"高危24条双盲复核表-{role}"
    output = OUT_DIR / filename
    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)

    checked = load_workbook(output)
    sheet = checked["高危24条"]
    assert sheet.max_row == 28, sheet.max_row
    assert sheet["A5"].value == "G025", sheet["A5"].value
    assert sheet["J5"].value is None
    print(f"saved={output} rows={sheet.max_row - 4}")



def main() -> int:
    """
    生成 A/B 两份文件
        :return: 退出码
    """
    build("复核人 A：财务（任意一位 finance）", "high_risk_review_finance.xlsx")
    build("复核人 B：老板（boss）", "high_risk_review_boss.xlsx")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
