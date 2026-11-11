"""
生成财务复核工作簿（P0 #1：100 条金标准草稿）
    输入：
        evaluation/cases/golden_v1.json
        evaluation/recordings/preview_5.json
        evaluation/reports/preview_5.json
        evaluation/reports/golden_mock.json
    输出：
        evaluation/reports/golden_review.xlsx
    用途：
        1. 5 条真实预览的“期望 vs 实际”对照与签字；
        2. 100 条金标准草稿的双人独立复核表；
        3. 指标口径与复核流程说明。
"""
import json
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation



BASE = Path(__file__).resolve().parent
CASES_FILE = BASE / "cases" / "golden_v1.json"
RECORDING_FILE = BASE / "recordings" / "preview_5.json"
PREVIEW_REPORT = BASE / "reports" / "preview_5.json"
MOCK_REPORT = BASE / "reports" / "golden_mock.json"
OUTPUT = BASE / "reports" / "golden_review.xlsx"

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
SUB_FILL = PatternFill("solid", fgColor="D9E2F3")
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
OK_FILL = PatternFill("solid", fgColor="C6EFCE")
BAD_FILL = PatternFill("solid", fgColor="FFC7CE")
GRAY_FILL = PatternFill("solid", fgColor="F2F2F2")
WHITE_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Microsoft YaHei", size=10)
BOLD_FONT = Font(name="Microsoft YaHei", size=10, bold=True)
TITLE_FONT = Font(name="Microsoft YaHei", size=15, bold=True, color="1F4E78")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)



# 读取 JSON
def load_json(path: Path):
    """
    读取 JSON 文件
        :param path: 文件路径
        :return: Python 对象
    """
    return json.loads(path.read_text(encoding="utf-8"))



# 写标题
def write_title(ws, text: str, subtitle: str, last_col: int):
    """
    写顶部标题与说明
        :param ws: 工作表
        :param text: 标题
        :param subtitle: 说明
        :param last_col: 最后一列序号
    """
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    cell = ws.cell(row=1, column=1, value=text)
    cell.font = TITLE_FONT
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    sub = ws.cell(row=2, column=1, value=subtitle)
    sub.font = Font(name="Microsoft YaHei", size=9, color="595959")
    sub.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 30



# 写表头
def write_header(ws, row: int, headers: list[str]):
    """
    写表头并套用样式
        :param ws: 工作表
        :param row: 行号
        :param headers: 表头
    """
    for index, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=index, value=text)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[row].height = 34



# 写单元格
def write_cell(ws, row: int, col: int, value, *, fill=None, bold=False, wrap=True, center=False):
    """
    写一个带样式的单元格
        :param ws: 工作表
        :param row: 行
        :param col: 列
        :param value: 值
        :param fill: 填充
        :param bold: 是否加粗
        :param wrap: 是否换行
        :param center: 是否居中
    """
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = BOLD_FONT if bold else BODY_FONT
    cell.border = BORDER
    cell.alignment = Alignment(
        vertical="top",
        wrap_text=wrap,
        horizontal="center" if center else "left",
    )
    if fill is not None:
        cell.fill = fill



# 设置列宽
def set_widths(ws, widths: list[float]):
    """
    设置列宽
        :param ws: 工作表
        :param widths: 宽度列表
    """
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width



# 列表下拉
def add_list_validation(ws, cell_range: str, options: str):
    """
    添加下拉校验
        :param ws: 工作表
        :param cell_range: 单元格范围
        :param options: 逗号分隔的选项
    """
    validation = DataValidation(type="list", formula1=f'"{options}"', allow_blank=True)
    ws.add_data_validation(validation)
    validation.add(cell_range)



# 预览页
def build_preview_sheet(ws, cases: list[dict], outcomes: dict, report: dict):
    """
    构建 5 条真实预览复核页
        :param ws: 工作表
        :param cases: 全部案例
        :param outcomes: 录制结果
        :param report: 真实评测报告
    """
    headers = [
        "案例编号", "标题", "分类", "金额", "期望风险", "期望决策",
        "实际风险", "实际决策", "风险分", "决策一致", "风险一致",
        "实际风险点", "缺失 finding", "财务复核风险", "财务复核决策",
        "标签是否正确", "差异说明", "复核人", "复核日期", "状态",
    ]
    write_title(
        ws,
        "AI 预审 5 条真实预览 — 财务复核表",
        "模型 deepseek-flash / Prompt risk_review_v3_policy；实际结果为真实调用。"
        "请只填写黄色区域，标签仍为 draft，确认前不得作为上线依据。",
        len(headers),
    )
    write_header(ws, 4, headers)

    report_map = {item["case_id"]: item for item in report.get("results", [])}
    preview_ids = [item for item in ("G001", "G025", "G033", "G061", "G065") if item in outcomes]
    case_map = {case["case_id"]: case for case in cases}

    row = 5
    for case_id in preview_ids:
        case = case_map[case_id]
        outcome = outcomes[case_id]
        result = report_map.get(case_id, {})
        expected = case.get("expected", {})
        findings = outcome.get("findings") or []
        finding_text = "\n".join(
            f"[{item.get('level')}] {item.get('message')}" for item in findings
        ) or "无"

        values = [
            case_id,
            case.get("title"),
            case.get("category"),
            case["context"]["expense"]["total_amount"],
            expected.get("risk_level"),
            expected.get("decision"),
            outcome.get("risk_level"),
            result.get("actual_decision"),
            outcome.get("risk_score"),
            "是" if result.get("decision_match") else "否",
            "是" if result.get("risk_match") else "否",
            finding_text,
            "、".join(result.get("missing_findings") or []) or "无",
            None,
            None,
            None,
            None,
            None,
            None,
            "待复核",
        ]
        for col, value in enumerate(values, start=1):
            fill = None
            if col in (14, 15, 16, 17, 18, 19, 20):
                fill = REVIEW_FILL
            elif col == 10:
                fill = OK_FILL if result.get("decision_match") else BAD_FILL
            elif col == 11:
                fill = OK_FILL if result.get("risk_match") else BAD_FILL
            write_cell(
                ws, row, col, value,
                fill=fill,
                center=col in (1, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 19, 20),
            )
        ws.row_dimensions[row].height = 72
        row += 1

    set_widths(ws, [
        10, 26, 14, 10, 10, 14, 10, 16, 8, 9, 9,
        42, 16, 14, 18, 11, 30, 10, 12, 10,
    ])
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:T{row - 1}"
    ws.sheet_view.showGridLines = False

    add_list_validation(ws, f"N5:N{row - 1}", "low,mid,high")
    add_list_validation(ws, f"O5:O{row - 1}", "fast_track,manual_review,suggest_reject")
    add_list_validation(ws, f"P5:P{row - 1}", "是,否")
    add_list_validation(ws, f"T5:T{row - 1}", "待复核,已复核,需讨论")

    note_row = row + 1
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=len(headers))
    note = ws.cell(
        row=note_row,
        column=1,
        value="填写说明：财务复核风险=low/mid/high；复核决策=fast_track/manual_review/suggest_reject；"
              "标签是否正确=是/否；状态=待复核/已复核/需讨论。",
    )
    note.font = Font(name="Microsoft YaHei", size=9, color="7F6000")
    note.fill = GRAY_FILL
    note.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[note_row].height = 26



# 100 条复核页
def build_golden_sheet(ws, cases: list[dict]):
    """
    构建 100 条金标准双人复核页
        :param ws: 工作表
        :param cases: 案例列表
    """
    headers = [
        "案例编号", "标题", "分类", "金额", "工程草案风险", "工程草案决策",
        "必须命中", "禁止项", "复核风险", "复核决策",
        "双标A风险", "双标A决策", "双标B风险", "双标B决策",
        "双标是否一致", "差异说明", "复核人", "复核日期", "状态", "备注",
    ]
    write_title(
        ws,
        "100 条金标准草稿 — 双人独立复核表",
        "工程侧合成场景，label_status=draft；请财务 A/B 独立填写，不一致的案例进入讨论并形成最终冻结标签。"
        "复核完成前不要用本表作为上线依据。",
        len(headers),
    )
    write_header(ws, 4, headers)

    row = 5
    for case in cases:
        expected = case.get("expected", {})
        values = [
            case.get("case_id"),
            case.get("title"),
            case.get("category"),
            case["context"]["expense"]["total_amount"],
            expected.get("risk_level"),
            expected.get("decision"),
            "、".join(expected.get("must_findings") or []) or "无",
            "、".join(expected.get("must_not") or []) or "无",
            None, None, None, None, None, None, None, None, None, None,
            "待复核",
            case.get("note", ""),
        ]
        for col, value in enumerate(values, start=1):
            fill = None
            if 9 <= col <= 18:
                fill = REVIEW_FILL
            elif col in (5, 6):
                fill = GRAY_FILL
            write_cell(
                ws, row, col, value,
                fill=fill,
                center=col in (1, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 18, 19),
            )
        ws.row_dimensions[row].height = 40
        row += 1

    set_widths(ws, [
        10, 30, 14, 10, 12, 16, 18, 12, 10, 16,
        10, 16, 10, 16, 11, 30, 10, 12, 10, 30,
    ])
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:T{row - 1}"
    ws.sheet_view.showGridLines = False

    add_list_validation(ws, f"I5:I{row - 1}", "low,mid,high")
    add_list_validation(ws, f"J5:J{row - 1}", "fast_track,manual_review,suggest_reject")
    add_list_validation(ws, f"K5:K{row - 1}", "low,mid,high")
    add_list_validation(ws, f"L5:L{row - 1}", "fast_track,manual_review,suggest_reject")
    add_list_validation(ws, f"M5:M{row - 1}", "low,mid,high")
    add_list_validation(ws, f"N5:N{row - 1}", "fast_track,manual_review,suggest_reject")
    add_list_validation(ws, f"O5:O{row - 1}", "是,否,待讨论")
    add_list_validation(ws, f"S5:S{row - 1}", "待复核,已复核,需讨论")



# 指标说明页
def build_metrics_sheet(ws, preview: dict, mock: dict):
    """
    构建指标与流程说明页
        :param ws: 工作表
        :param preview: 5 条真实报告
        :param mock: 100 条 mock 报告
    """
    write_title(
        ws,
        "评测指标与复核流程",
        "真实预览用于验证链路；100 条标签需财务双标后才能冻结 test 集。",
        4,
    )
    headers = ["指标", "5 条真实预览", "100 条 mock 草稿", "说明"]
    write_header(ws, 4, headers)

    rows = [
        ("案例数", preview.get("total"), mock.get("total"), "真实预览只跑了 5 条代表性案例"),
        ("决策一致率", f"{preview.get('decision_accuracy', 0):.2%}", f"{mock.get('decision_accuracy', 0):.2%}", "与人工标签一致比例"),
        ("风险等级一致率", f"{preview.get('risk_level_accuracy', 0):.2%}", f"{mock.get('risk_level_accuracy', 0):.2%}", "low/mid/high 一致比例"),
        ("高危漏放率", f"{preview.get('high_risk_miss_rate', 0):.2%}", f"{mock.get('high_risk_miss_rate', 0):.2%}", "人工 high 被判 low/fast_track"),
        ("降级率", f"{preview.get('fallback_rate', 0):.2%}", f"{mock.get('fallback_rate', 0):.2%}", "规则兜底比例"),
        ("finding 召回率", f"{preview.get('finding_recall', 0):.2%}", f"{mock.get('finding_recall', 0):.2%}", "必须命中的风险点被命中比例"),
        ("平均耗时（ms）", preview.get("avg_latency_ms", 0), mock.get("avg_latency_ms", 0), "真实/回放结果的平均值"),
        ("平均 token", preview.get("avg_tokens", 0), mock.get("avg_tokens", 0), "输入+输出 token"),
    ]
    for index, (name, real, fake, note) in enumerate(rows, start=5):
        write_cell(ws, index, 1, name, bold=True)
        write_cell(ws, index, 2, real, center=True)
        write_cell(ws, index, 3, fake, center=True)
        write_cell(ws, index, 4, note)
        ws.row_dimensions[index].height = 24

    set_widths(ws, [22, 18, 20, 64])

    start = len(rows) + 7
    ws.merge_cells(start_row=start, start_column=1, end_row=start, end_column=4)
    title = ws.cell(row=start, column=1, value="复核流程")
    title.font = BOLD_FONT
    title.fill = SUB_FILL

    steps = [
        "1. 财务 A/B 各自独立填写“双标A/B”四列，不参考工程草案，减少锚定偏差。",
        "2. 对双标不一致的案例，在“差异说明”记录分歧点，组织 15 分钟讨论并给出最终标签。",
        "3. 将最终标签回写到 golden_v1.json 的 expected 字段，label_status 改为 reviewed，冻结为 test 集。",
        "4. 冻结后再跑完整 100 条真实 LLM 评测；此前只保留 5 条预览，不扩大到全量。",
        "5. 评测报告与录制文件归档到 evaluation/reports 与 evaluation/recordings，按版本命名。",
    ]
    for offset, text in enumerate(steps, start=1):
        row = start + offset
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = BODY_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[row].height = 30

    warning_row = start + len(steps) + 2
    ws.merge_cells(start_row=warning_row, start_column=1, end_row=warning_row, end_column=4)
    warning = ws.cell(
        row=warning_row,
        column=1,
        value="注意：100 条 mock 的 100% 指标来自 simulated 结果，不代表真实 LLM 准确率；"
              "5 条真实预览也只覆盖低/中/高三档，不能替代全量评测。",
    )
    warning.font = Font(name="Microsoft YaHei", size=10, color="C00000", bold=True)
    warning.fill = BAD_FILL
    warning.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[warning_row].height = 40
    ws.sheet_view.showGridLines = False



# 填写说明页
def build_guide_sheet(ws):
    """
    构建填写说明页
        :param ws: 工作表
    """
    write_title(
        ws,
        "填写说明与字段字典",
        "本工作簿用于 100 条金标准草稿的财务/业务复核，不包含生产数据。",
        2,
    )
    headers = ["字段", "说明"]
    write_header(ws, 4, headers)

    rows = [
        ("复核风险", "low / mid / high；按制度与业务实质判断，不参考工程草案"),
        ("复核决策", "fast_track / manual_review / suggest_reject；只给建议，不自动审批"),
        ("双标A / 双标B", "两位复核人独立填写；不一致时在差异说明记录"),
        ("双标是否一致", "是 / 否 / 待讨论"),
        ("标签是否正确（预览页）", "是：期望标签与实际结果一致；否：需说明差异"),
        ("状态", "待复核 / 已复核 / 需讨论"),
        ("复核日期", "建议 YYYY-MM-DD"),
        ("工程草案", "工程侧按场景生成的 expected，仅作为对照，不是最终标签"),
        ("must_findings", "该案例必须命中的风险点代码，用于计算召回率"),
        ("must_not", "禁止出现的决策，例如 high 风险案例禁止 fast_track"),
    ]
    row = 5
    for name, note in rows:
        write_cell(ws, row, 1, name, bold=True)
        write_cell(ws, row, 2, note)
        ws.row_dimensions[row].height = 30
        row += 1

    set_widths(ws, [30, 90])
    ws.sheet_view.showGridLines = False



# 主流程
def main() -> int:
    """
    生成并校验工作簿
        :return: 退出码
    """
    cases = load_json(CASES_FILE)
    recording = load_json(RECORDING_FILE)
    preview = load_json(PREVIEW_REPORT)
    mock = load_json(MOCK_REPORT)
    outcomes = recording.get("results") or {}

    wb = Workbook()
    wb.active.title = "预览5条"
    wb.properties.title = "AI 预审金标准复核表"
    wb.properties.subject = "100 条金标准草稿 + 5 条真实预览"
    wb.properties.creator = "expense-audit-system"

    build_preview_sheet(wb.active, cases, outcomes, preview)
    build_golden_sheet(wb.create_sheet("100条待复核"), cases)
    build_metrics_sheet(wb.create_sheet("指标与流程"), preview, mock)
    build_guide_sheet(wb.create_sheet("填写说明"))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT)

    # 重新打开校验关键内容
    checked = load_workbook(OUTPUT, data_only=False)
    assert checked.sheetnames == ["预览5条", "100条待复核", "指标与流程", "填写说明"], checked.sheetnames
    preview_sheet = checked["预览5条"]
    golden_sheet = checked["100条待复核"]
    assert preview_sheet.max_row >= 9
    assert golden_sheet.max_row >= 104
    assert preview_sheet["A5"].value == "G001"
    assert golden_sheet["A5"].value == "G001"
    assert golden_sheet["A104"].value == "G100"
    print(f"saved={OUTPUT}")
    print(f"sheets={checked.sheetnames}")
    print(f"preview_rows={preview_sheet.max_row - 5} golden_rows={golden_sheet.max_row - 4}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
