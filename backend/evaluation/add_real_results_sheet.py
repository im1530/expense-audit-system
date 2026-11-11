"""
把 100 条真实评测结果写入复核工作簿（P0 #2）
    输入：
        evaluation/reports/golden_review.xlsx
        evaluation/reports/real_100.json
        evaluation/recordings/real_100.json
        evaluation/reports/preview_5.json
        evaluation/reports/golden_mock.json
    动作：
        - 新增“100条真实结果”Sheet；
        - 重建“指标与流程”Sheet，加入真实 100 条指标与差异摘要；
        - 不改“100条待复核”的确认状态，避免覆盖人工结论。
"""
import json
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter



BASE = Path(__file__).resolve().parent
WORKBOOK = BASE / "reports" / "golden_review.xlsx"
REAL_REPORT = BASE / "reports" / "real_100.json"
REAL_RECORDING = BASE / "recordings" / "real_100.json"
PREVIEW_REPORT = BASE / "reports" / "preview_5.json"
MOCK_REPORT = BASE / "reports" / "golden_mock.json"

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



def load_json(path: Path):
    """
    读取 JSON
        :param path: 路径
        :return: Python 对象
    """
    return json.loads(path.read_text(encoding="utf-8"))



def title(ws, text: str, subtitle: str, last_col: int):
    """
    写标题
        :param ws: 工作表
        :param text: 标题
        :param subtitle: 说明
        :param last_col: 列数
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
    ws.row_dimensions[2].height = 34



def header(ws, row: int, headers: list[str]):
    """
    写表头
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



def cell(ws, row: int, col: int, value, *, fill=None, center=False, bold=False, wrap=True):
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
    target = ws.cell(row=row, column=col, value=value)
    target.font = BOLD_FONT if bold else BODY_FONT
    target.border = BORDER
    target.alignment = Alignment(
        vertical="top", wrap_text=wrap,
        horizontal="center" if center else "left",
    )
    if fill is not None:
        target.fill = fill



def widths(ws, values: list[float]):
    """
    设置列宽
        :param ws: 工作表
        :param values: 宽度列表
    """
    for index, width in enumerate(values, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width



def build_results_sheet(ws, report: dict, recording: dict):
    """
    构建 100 条真实结果页
        :param ws: 工作表
        :param report: 真实报告
        :param recording: 真实录制
    """
    headers = [
        "案例编号", "标题", "分类", "期望风险", "期望决策", "实际风险", "实际决策",
        "风险分", "决策一致", "风险一致", "禁止决策违规", "缺失 finding",
        "实际风险点", "耗时ms", "token", "备注",
    ]
    title(
        ws,
        "100 条真实 LLM 评测结果",
        "模型 deepseek-flash / Prompt risk_review_v3_policy；真实调用 100 条，"
        "红色为不一致或违规，绿色为一致。标签为已确认的冻结 test 集。",
        len(headers),
    )
    header(ws, 4, headers)

    outcomes = recording.get("results") or {}
    row = 5
    for item in report.get("results", []):
        case_id = item["case_id"]
        outcome = outcomes.get(case_id, {})
        findings = outcome.get("findings") or []
        finding_text = "\n".join(
            f"[{f.get('level')}] {f.get('message')}" for f in findings
        ) or "无"
        values = [
            case_id,
            item.get("title"),
            item.get("category"),
            item.get("expected_risk_level"),
            item.get("expected_decision"),
            item.get("actual_risk_level"),
            item.get("actual_decision"),
            outcome.get("risk_score"),
            "是" if item.get("decision_match") else "否",
            "是" if item.get("risk_match") else "否",
            "是" if item.get("must_not_violated") else "否",
            "、".join(item.get("missing_findings") or []) or "无",
            finding_text,
            item.get("latency_ms"),
            (item.get("prompt_tokens") or 0) + (item.get("completion_tokens") or 0),
            item.get("decision_reason") or "",
        ]
        for col, value in enumerate(values, start=1):
            fill = None
            if col == 9:
                fill = OK_FILL if item.get("decision_match") else BAD_FILL
            elif col == 10:
                fill = OK_FILL if item.get("risk_match") else BAD_FILL
            elif col == 11 and item.get("must_not_violated"):
                fill = BAD_FILL
            cell(
                ws, row, col, value, fill=fill,
                center=col in (1, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15),
            )
        ws.row_dimensions[row].height = 62
        row += 1

    widths(ws, [10, 30, 14, 10, 16, 10, 16, 8, 9, 9, 12, 16, 48, 9, 8, 36])
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:P{row - 1}"
    ws.sheet_view.showGridLines = False



def build_metrics_sheet(ws, preview: dict, mock: dict, real: dict):
    """
    重建指标与流程页
        :param ws: 工作表
        :param preview: 5 条真实预览
        :param mock: 100 条 mock
        :param real: 100 条真实
    """
    headers = ["指标", "5 条真实预览", "100 条 mock", "100 条真实", "说明"]
    title(
        ws,
        "评测指标与差异摘要",
        "5 条预览用于链路验证；100 条真实评测使用已确认的冻结标签。"
        "mock 的 100% 来自模拟结果，不代表真实准确率。",
        len(headers),
    )
    header(ws, 4, headers)

    rows = [
        ("案例数", preview.get("total"), mock.get("total"), real.get("total"), "真实预览 5 条 / 冻结集 100 条"),
        ("决策一致率", f"{preview.get('decision_accuracy', 0):.2%}", f"{mock.get('decision_accuracy', 0):.2%}", f"{real.get('decision_accuracy', 0):.2%}", "与人工标签一致比例"),
        ("风险等级一致率", f"{preview.get('risk_level_accuracy', 0):.2%}", f"{mock.get('risk_level_accuracy', 0):.2%}", f"{real.get('risk_level_accuracy', 0):.2%}", "low/mid/high 一致比例"),
        ("高危漏放率", f"{preview.get('high_risk_miss_rate', 0):.2%}", f"{mock.get('high_risk_miss_rate', 0):.2%}", f"{real.get('high_risk_miss_rate', 0):.2%}", "人工 high 被判 low/fast_track"),
        ("finding 召回率", f"{preview.get('finding_recall', 0):.2%}", f"{mock.get('finding_recall', 0):.2%}", f"{real.get('finding_recall', 0):.2%}", "必须命中的风险点被命中比例"),
        ("降级率", f"{preview.get('fallback_rate', 0):.2%}", f"{mock.get('fallback_rate', 0):.2%}", f"{real.get('fallback_rate', 0):.2%}", "规则兜底比例"),
        ("平均耗时（ms）", preview.get("avg_latency_ms", 0), mock.get("avg_latency_ms", 0), real.get("avg_latency_ms", 0), "真实/回放结果平均值"),
        ("平均 token", preview.get("avg_tokens", 0), mock.get("avg_tokens", 0), real.get("avg_tokens", 0), "输入+输出 token"),
        ("禁止决策违规", preview.get("must_not_violations", 0), mock.get("must_not_violations", 0), real.get("must_not_violations", 0), "期望禁止 fast_track 但实际给出"),
    ]
    for index, values in enumerate(rows, start=5):
        for col, value in enumerate(values, start=1):
            cell(ws, index, col, value, bold=(col == 1), center=(col in (2, 3, 4)))
        ws.row_dimensions[index].height = 24

    start = 16
    ws.merge_cells(start_row=start, start_column=1, end_row=start, end_column=5)
    section = ws.cell(row=start, column=1, value="真实 100 条差异摘要（需业务确认后调整标签或模型）")
    section.font = BOLD_FONT
    section.fill = SUB_FILL

    notes = [
        ("决策不一致", "18 条：主要是低风险案例偏保守、Prompt 注入案例被判中风险、拆票案例被判高风险"),
        ("风险不一致", "15 条：低风险→中风险、Prompt 注入→中风险、拆票中风险→高风险"),
        ("高危漏放", "0 条：24 条高危案例中没有被判为 low/fast_track"),
        ("禁止决策违规", "1 条：G073（ai_fallback 场景）真实模型未降级，给出 fast_track"),
        ("主要问题 1", "低风险案例缺少费用用途说明时，模型倾向 manual_review；需确认标签是否应维持 fast_track"),
        ("主要问题 2", "Prompt 注入案例中模型把注入文本当作风险，导致误报；需补 Prompt 边界或调整标签"),
        ("主要问题 3", "拆票案例模型给 high，标签是 mid；需财务确认严重程度"),
        ("主要问题 4", "ai_fallback 案例无法在真实模式强制触发；建议移出 real 测试集，只在 mock/降级专项测试中使用"),
    ]
    for offset, (name, text) in enumerate(notes, start=1):
        row = start + offset
        cell(ws, row, 1, name, bold=True, fill=GRAY_FILL)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
        cell(ws, row, 2, text)
        ws.row_dimensions[row].height = 34

    widths(ws, [22, 18, 18, 18, 70])
    ws.sheet_view.showGridLines = False



def main() -> int:
    """
    写入真实结果 Sheet 并重建指标页
        :return: 退出码
    """
    report = load_json(REAL_REPORT)
    recording = load_json(REAL_RECORDING)
    preview = load_json(PREVIEW_REPORT)
    mock = load_json(MOCK_REPORT)

    wb = load_workbook(WORKBOOK)
    if "100条真实结果" in wb.sheetnames:
        del wb["100条真实结果"]
    results_sheet = wb.create_sheet("100条真实结果", 2)
    build_results_sheet(results_sheet, report, recording)

    if "指标与流程" in wb.sheetnames:
        del wb["指标与流程"]
    metrics_sheet = wb.create_sheet("指标与流程", 3)
    build_metrics_sheet(metrics_sheet, preview, mock, report)

    wb.save(WORKBOOK)

    checked = load_workbook(WORKBOOK)
    assert "100条真实结果" in checked.sheetnames
    assert checked["100条真实结果"].max_row == 104
    assert checked["100条真实结果"]["A5"].value == "G001"
    assert checked["100条真实结果"]["A104"].value == "G100"
    print(f"updated={WORKBOOK}")
    print(f"sheets={checked.sheetnames}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
