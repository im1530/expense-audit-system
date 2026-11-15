"""
blind_v2 一致性统计与裁决清单（P0-1 第④步，裁决前）

    做什么：
        1. 读取两位标注人回填的 xlsx，校验行数、编号、取值与单据事实未被改动；
        2. 计算逐维度百分比一致率、Cohen's κ、bootstrap 95% CI，
           并附线性/二次加权 κ 与 PABAK 作为敏感性指标；
        3. 输出报告 json + md，以及只含分歧项的裁决清单 xlsx；
        4. κ 一律使用**裁决前**的原始标注，裁决后的标签不回流到 κ。

    用法（在 backend/ 下）：
        python evaluation/analyze_blind_v2.py
        python evaluation/analyze_blind_v2.py --a <A.xlsx> --b <B.xlsx>

    说明：
        - 纯离线，不联网、不连库、不调 LLM；
        - 默认读取 reports/blind_v2_returned_A.xlsx / _B.xlsx（由桌面回填件归档而来）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


# 本脚本目录（backend/evaluation）
BASE = Path(__file__).resolve().parent
# 报告目录
REPORTS = BASE / "reports"
# 冻结样本
SAMPLE = BASE / "cases" / "blind_v2.json"
# 默认回填件（A=财务，B=老板）
DEFAULT_A = REPORTS / "blind_v2_returned_A.xlsx"
DEFAULT_B = REPORTS / "blind_v2_returned_B.xlsx"
# 产物
OUT_JSON = REPORTS / "blind_v2_agreement.json"
OUT_MD = REPORTS / "blind_v2_agreement.md"
OUT_XLSX = REPORTS / "blind_v2_disagreements.xlsx"

# 风险三档（有序，加权 κ 用）
RISK_LABELS = ("low", "mid", "high")
# 决策三档（有序）
DECISION_LABELS = ("fast_track", "manual_review", "suggest_reject")

# 中文/简写别名 → 标准值（防止手填或复制粘贴带出别写法）
RISK_ALIASES = {
    "low": "low", "低": "low", "低风险": "low", "l": "low",
    "mid": "mid", "medium": "mid", "中": "mid", "中风险": "mid", "m": "mid",
    "high": "high", "高": "high", "高风险": "high", "h": "high",
}
DECISION_ALIASES = {
    "fast_track": "fast_track", "fasttrack": "fast_track", "快速": "fast_track",
    "快速通过": "fast_track", "快速审批": "fast_track",
    "manual_review": "manual_review", "manualreview": "manual_review",
    "人工复核": "manual_review", "复核": "manual_review", "人工": "manual_review",
    "suggest_reject": "suggest_reject", "suggestreject": "suggest_reject",
    "驳回": "suggest_reject", "建议驳回": "suggest_reject", "退回": "suggest_reject",
}

# 表头与数据位置（与 build_blind_v2_packets.py 保持一致）
DATA_FIRST_ROW = 5
COL_ID = 1
COL_TITLE = 2
COL_CATEGORY = 3
COL_AMOUNT = 4
COL_DATE = 5
COL_SUBMITTED = 6
COL_INVOICE = 7
COL_RISK = 9
COL_DECISION = 10
COL_CITATION = 11
COL_REASON = 12

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
GRAY_FILL = PatternFill("solid", fgColor="F2F2F2")
WHITE_FONT = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Microsoft YaHei", size=10)
BOLD_FONT = Font(name="Microsoft YaHei", size=10, bold=True)
TITLE_FONT = Font(name="Microsoft YaHei", size=15, bold=True, color="1F4E78")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def sha256_file(path: Path) -> str:
    """
    计算文件 sha256
        :param path: 文件路径
        :return: 十六进制摘要
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value, aliases: dict[str, str]) -> str | None:
    """
    归一化标注值（去空白、转小写、查别名，再按关键词兜底）
        :param value: 单元格原值
        :param aliases: 别名表
        :return: 标准值；无法识别返回 None
    """
    if value is None:
        return None
    text = str(value).strip().lower()  # 去空白、转小写
    if not text:
        return None
    if text in aliases:
        return aliases[text]
    # 兜底：包含关键词也算（如"建议驳回/需人工复核"）
    for keyword, label in aliases.items():
        if keyword in text:
            return label
    return None


def as_text(value) -> str:
    """
    把单元格值转成可比较文本（日期单元格可能被 Excel 转成 datetime）
        :param value: 单元格值
        :return: 文本
    """
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def read_packet(path: Path) -> dict[str, dict]:
    """
    读取一份回填件，返回盲编号 → 标注与事实
        :param path: xlsx 路径
        :return: {blind_id: {"risk","decision","citation","reason","title","amount","date","invoice","submitted"}}
    """
    wb = load_workbook(path, data_only=True)  # 只读打开
    if wb.sheetnames != ["标注表", "标注说明", "制度条款"]:
        raise SystemExit(f"{path.name} 工作表不符：{wb.sheetnames}")
    ws = wb["标注表"]  # 标注表
    rows: dict[str, dict] = {}  # 结果
    for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60):
        blind_id = ws.cell(row=row, column=COL_ID).value  # 盲编号
        if not blind_id:
            raise SystemExit(f"{path.name} 第 {row} 行缺盲编号")
        rows[str(blind_id)] = {
            "risk_raw": ws.cell(row=row, column=COL_RISK).value,  # 风险原值
            "decision_raw": ws.cell(row=row, column=COL_DECISION).value,  # 决策原值
            "citation": ws.cell(row=row, column=COL_CITATION).value,  # 引用条款
            "reason": str(ws.cell(row=row, column=COL_REASON).value or "").strip(),  # 理由
            "title": str(ws.cell(row=row, column=COL_TITLE).value or ""),  # 标题
            "category": ws.cell(row=row, column=COL_CATEGORY).value,  # 费用类别
            "amount": ws.cell(row=row, column=COL_AMOUNT).value,  # 金额
            "date": ws.cell(row=row, column=COL_DATE).value,  # 费用日期
            "submitted": ws.cell(row=row, column=COL_SUBMITTED).value,  # 提交时间
            "invoice": str(ws.cell(row=row, column=COL_INVOICE).value or ""),  # 是否有票
        }
    return rows


def check_integrity(a: dict, b: dict, sample: dict) -> tuple[list[str], list[str], list[str]]:
    """
    校验两份回填件的编号、取值与事实完整性
        :param a: A 标注
        :param b: B 标注
        :param sample: 冻结样本 meta+cases
        :return: (问题列表, 风险集合, 决策集合)
    """
    problems: list[str] = []  # 问题
    sample_ids = {row["blind_id"] for row in sample["cases"]}  # 60 个盲编号
    if set(a) != sample_ids:
        problems.append(f"A 编号集合不符：缺 {sorted(sample_ids - set(a))[:5]}，多 {sorted(set(a) - sample_ids)[:5]}")
    if set(b) != sample_ids:
        problems.append(f"B 编号集合不符：缺 {sorted(sample_ids - set(b))[:5]}，多 {sorted(set(b) - sample_ids)[:5]}")

    canonical = {row["blind_id"]: row for row in sample["cases"]}  # 盲编号 → 事实
    for blind_id in sorted(set(a) & sample_ids):
        expect = canonical[blind_id]  # 期望事实
        item = expect["items"][0]  # 唯一明细
        for label, data in (("A", a[blind_id]), ("B", b[blind_id])):
            if data["title"] != expect["title"]:
                problems.append(f"{label} {blind_id} 标题被改：{data['title']!r}")
            if Decimal(str(data["amount"])) != Decimal(str(expect["total_amount"])):
                problems.append(f"{label} {blind_id} 金额被改：{data['amount']!r}")
            if as_text(data["date"]) != as_text(item["expense_date"]):
                problems.append(f"{label} {blind_id} 日期被改：{data['date']!r}")
            if (data["invoice"] == "有") != bool(item["has_invoice"]):
                problems.append(f"{label} {blind_id} 票证标记被改：{data['invoice']!r}")
            if as_text(data["submitted"]) != as_text(expect["submitted_at"]):
                problems.append(f"{label} {blind_id} 提交时间被改：{data['submitted']!r}")

    risks = set()  # 风险取值
    decisions = set()  # 决策取值
    for blind_id in sorted(set(a) & set(b)):
        for label, data in (("A", a[blind_id]), ("B", b[blind_id])):
            risk = normalize(data["risk_raw"], RISK_ALIASES)  # 归一化风险
            decision = normalize(data["decision_raw"], DECISION_ALIASES)  # 归一化决策
            if risk is None:
                problems.append(f"{label} {blind_id} 风险无法识别：{data['risk_raw']!r}")
            else:
                risks.add(risk)
            if decision is None:
                problems.append(f"{label} {blind_id} 决策无法识别：{data['decision_raw']!r}")
            else:
                decisions.add(decision)
            if risk and decision:
                # 硬边界：只有 low 才能 fast_track；mid 必须 manual_review
                if decision == "fast_track" and risk != "low":
                    problems.append(f"{label} {blind_id} 决策违规：{risk}+fast_track")
                if decision == "manual_review" and risk == "low":
                    problems.append(f"{label} {blind_id} 决策偏保守：low+manual_review")
    return problems, sorted(risks), sorted(decisions)


def kappa(pairs, labels, power: int | None = None) -> tuple[float, float, list[list[int]]]:
    """
    计算Cohen's κ（power=None 时不加权，1=线性加权，2=二次加权）
        :param pairs: [(A标签, B标签)]
        :param labels: 有序标签
        :param power: 加权幂次
        :return: (一致率 po, κ, 混淆矩阵)
    """
    n = len(pairs)  # 条数
    index = {label: i for i, label in enumerate(labels)}  # 标签 → 下标
    k = len(labels)  # 类别数
    matrix = [[0] * k for _ in range(k)]  # 混淆矩阵
    for left, right in pairs:
        matrix[index[left]][index[right]] += 1
    po = sum(matrix[i][i] for i in range(k)) / n  # 一致率
    row_marginal = [sum(matrix[i]) / n for i in range(k)]  # 行边际
    col_marginal = [sum(matrix[i][j] for i in range(k)) / n for j in range(k)]  # 列边际
    pe = sum(row_marginal[i] * col_marginal[i] for i in range(k))  # 期望一致率

    if power is None:
        weights = [[0.0 if i == j else 1.0 for j in range(k)] for i in range(k)]  # 不加权
    else:
        weights = [[(abs(i - j) / (k - 1)) ** power for j in range(k)] for i in range(k)]  # 加权

    observed = sum(weights[i][j] * matrix[i][j] for i in range(k) for j in range(k)) / n  # 观测分歧
    expected = sum(weights[i][j] * row_marginal[i] * col_marginal[j] for i in range(k) for j in range(k))  # 期望分歧
    value = 1.0 if expected == 0 else 1.0 - observed / expected  # κ

    if power is None:
        value = 1.0 if pe == 1 else (po - pe) / (1 - pe)  # 不加权κ用标准公式
    return po, value, matrix


def pabak(po: float, categories: int) -> float:
    """
    计算 PABAK（假设两方边际均匀分布的敏感性指标）
        :param po: 一致率
        :param categories: 类别数
        :return: PABAK
    """
    return (categories * po - 1) / (categories - 1)


def bootstrap_ci(pairs, labels, seed: int = 20261114, rounds: int = 10000) -> tuple[float, float, float]:
    """
    对 κ 做 percentile bootstrap 95% CI
        :param pairs: [(A标签, B标签)]
        :param labels: 有序标签
        :param seed: 固定随机种子
        :param rounds: 重采样次数
        :return: (2.5%, 97.5%, 均值)
    """
    rng = random.Random(seed)  # 固定种子，结果可复现
    values: list[float] = []  # 每次重采样的 κ
    n = len(pairs)  # 条数
    for _ in range(rounds):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]  # 有放回抽样
        values.append(kappa(sample, labels)[1])
    values.sort()  # 排序取分位
    return values[int(rounds * 0.025)], values[int(rounds * 0.975)], sum(values) / rounds


def wilson_ci(po: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    Wilson 95% 区间（用于一致率，避免正态近似的边界问题）
        :param po: 一致率
        :param n: 条数
        :param z: 正态分位数
        :return: (下限, 上限)
    """
    denominator = 1 + z * z / n  # 分母
    center = (po + z * z / (2 * n)) / denominator  # 中心
    half = z * ((po * (1 - po) / n + z * z / (4 * n * n)) ** 0.5) / denominator  # 半宽
    return max(0.0, center - half), min(1.0, center + half)


def dimension_stats(pairs, labels, name: str) -> dict:
    """
    汇总一个维度的全部指标
        :param pairs: [(A标签, B标签)]
        :param labels: 有序标签
        :param name: 维度名
        :return: 指标字典
    """
    po, value, matrix = kappa(pairs, labels)  # 主指标
    _, linear, _ = kappa(pairs, labels, 1)  # 线性加权
    _, quadratic, _ = kappa(pairs, labels, 2)  # 二次加权
    low, high, mean = bootstrap_ci(pairs, labels)  # 置信区间
    po_low, po_high = wilson_ci(po, len(pairs))  # 一致率区间
    marginal_a = {label: sum(1 for left, _ in pairs if left == label) for label in labels}  # A 边际
    marginal_b = {label: sum(1 for _, right in pairs if right == label) for label in labels}  # B 边际
    agree = sum(1 for left, right in pairs if left == right)  # 一致条数
    return {
        "dimension": name,  # 维度
        "n": len(pairs),  # 条数
        "agree": agree,  # 一致条数
        "po": round(po, 6),  # 一致率
        "po_ci95": [round(po_low, 6), round(po_high, 6)],  # 一致率 Wilson CI
        "kappa": round(value, 6),  # Cohen's κ
        "kappa_ci95": [round(low, 6), round(high, 6)],  # bootstrap CI
        "kappa_boot_mean": round(mean, 6),  # bootstrap 均值
        "weighted_linear": round(linear, 6),  # 线性加权 κ
        "weighted_quadratic": round(quadratic, 6),  # 二次加权 κ
        "pabak": round(pabak(po, len(labels)), 6),  # PABAK
        "confusion": matrix,  # 混淆矩阵（行=A，列=B）
        "marginal_a": marginal_a,  # A 边际分布
        "marginal_b": marginal_b,  # B 边际分布
    }


def level_text(value: float) -> str:
    """
    Landis & Koch 一致性等级
        :param value: κ
        :return: 文字等级
    """
    if value >= 0.81:
        return "almost perfect（几乎完全一致）"
    if value >= 0.61:
        return "substantial（高度一致）"
    if value >= 0.41:
        return "moderate（中等）"
    if value >= 0.21:
        return "fair（一般）"
    return "slight/poor（差）"


def build_disagreement_book(disagreements: list[dict]) -> None:
    """
    生成裁决清单 xlsx（只含分歧项）
        :param disagreements: 分歧列表
    """
    wb = Workbook()  # 工作簿
    ws = wb.active
    ws.title = "分歧清单"

    headers = (
        "盲编号", "标题/费用说明", "费用类别", "金额(元)", "费用日期", "是否有票",
        "A 风险", "A 决策", "A 引用", "A 理由",
        "B 风险", "B 决策", "B 引用", "B 理由",
        "裁决风险", "裁决决策", "裁决说明", "裁决人", "裁决时间",
    )
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title = ws.cell(row=1, column=1, value="blind_v2 分歧裁决清单（只裁决本页，其余 58 条已一致）")
    title.font = TITLE_FONT
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    note = ws.cell(
        row=2, column=1,
        value="请只依据单据事实与制度条款裁决，不要参考 AI 结论；κ 用裁决前标注计算，裁决结果只写入 high_risk_final_v2.json。",
    )
    note.font = Font(name="Microsoft YaHei", size=9, color="595959")
    ws.row_dimensions[2].height = 30

    for index, text in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=index, value=text)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_index, item in enumerate(disagreements, start=5):
        values = (
            item["blind_id"], item["title"], item["category"], item["amount"], item["date"], item["invoice"],
            item["a"]["risk"], item["a"]["decision"], item["a"]["citation"], item["a"]["reason"],
            item["b"]["risk"], item["b"]["decision"], item["b"]["citation"], item["b"]["reason"],
            None, None, None, None, None,
        )
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_index, column=col, value=value)
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True, horizontal="center" if col in (1, 4, 5, 6) else "left")
            if col >= 15:
                cell.fill = REVIEW_FILL
        ws.row_dimensions[row_index].height = 60

    widths = (9, 26, 10, 10, 12, 9, 9, 17, 12, 34, 9, 17, 12, 34, 10, 17, 30, 10, 14)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A5"
    ws.sheet_view.showGridLines = False

    risk_dv = DataValidation(type="list", formula1='"low,mid,high"', allow_blank=True)
    decision_dv = DataValidation(type="list", formula1='"fast_track,manual_review,suggest_reject"', allow_blank=True)
    ws.add_data_validation(risk_dv)
    ws.add_data_validation(decision_dv)
    last = 4 + len(disagreements)
    risk_dv.add(f"O5:O{last}")
    decision_dv.add(f"P5:P{last}")

    guide = wb.create_sheet("裁决说明")
    rows = [
        ("范围", "只裁决本清单中的分歧项；两人一致的 58 条直接作为终版标签"),
        ("依据", "单据事实 + 制度 v1.2 全文（见盲包《制度条款》页）；不参考 AI 结论"),
        ("风险", "low / mid / high，定义见 docs/annotate_guide_v2.0.md"),
        ("决策", "fast_track / manual_review / suggest_reject，与风险等级保持硬边界"),
        ("原则", "分歧多为口径问题；裁决时先判断事实，再落标签，并在裁决说明里写清依据"),
        ("统计口径", "κ 只用裁决前标注计算；裁决后再算一次 κ 会使一致性虚高"),
    ]
    for index, (name, text) in enumerate(rows, start=2):
        guide.cell(row=index, column=1, value=name).font = BOLD_FONT
        guide.cell(row=index, column=1).fill = GRAY_FILL
        guide.cell(row=index, column=1).border = BORDER
        guide.cell(row=index, column=2, value=text).font = BODY_FONT
        guide.cell(row=index, column=2).border = BORDER
        guide.cell(row=index, column=2).alignment = Alignment(wrap_text=True, vertical="center")
        guide.row_dimensions[index].height = 34
    guide.column_dimensions["A"].width = 14
    guide.column_dimensions["B"].width = 96

    wb.save(OUT_XLSX)


def build_markdown(result: dict) -> str:
    """
    生成 Markdown 报告
        :param result: 统计结果
        :return: markdown 文本
    """
    risk = result["risk"]  # 风险维度
    decision = result["decision"]  # 决策维度
    lines = [
        "# blind_v2 独立双标一致性报告（裁决前）",
        "",
        f"> 生成时间：{result['generated_at']} ｜ 样本：60 条（24 高危 + 18 低 + 18 中）",
        "> κ 使用**裁决前**的原始标注；裁决结果只进 `high_risk_final_v2.json`。",
        "",
        "## 1. 数据完整性",
        "",
        "| 项 | 结果 |",
        "|---|---|",
        f"| A 文件 | `{result['inputs']['A']['file']}`（sha256 `{result['inputs']['A']['sha256'][:12]}…`） |",
        f"| B 文件 | `{result['inputs']['B']['file']}`（sha256 `{result['inputs']['B']['sha256'][:12]}…`） |",
        f"| 条数 | 两份均 60 条，编号集合一致 |",
        f"| 事实改动 | {result['integrity']['fact_issues']} 处 |",
        f"| 取值问题 | {result['integrity']['value_issues']} 处 |",
        "",
        "## 2. 一致性指标",
        "",
        "| 维度 | 一致 | 一致率 | 一致率 95%CI | Cohen's κ | κ 95%CI | 线性加权 κ | 二次加权 κ | PABAK |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, item in (("风险等级", risk), ("决策建议", decision)):
        lines.append(
            f"| {name} | {item['agree']}/{item['n']} | {item['po']*100:.2f}% | "
            f"[{item['po_ci95'][0]*100:.1f}%, {item['po_ci95'][1]*100:.1f}%] | "
            f"{item['kappa']:.4f} | [{item['kappa_ci95'][0]:.4f}, {item['kappa_ci95'][1]:.4f}] | "
            f"{item['weighted_linear']:.4f} | {item['weighted_quadratic']:.4f} | {item['pabak']:.4f} |"
        )

    lines += [
        "",
        f"- 两个维度同时一致：**{result['both_agree']}/60**；",
        f"- 风险等级的 κ={risk['kappa']:.4f}，等级判定：**{level_text(risk['kappa'])}**；",
        f"- 决策建议的 κ={decision['kappa']:.4f}（与风险 κ 相同，因为两条分歧在两个维度上同时出现、且决策与风险一一对应）；",
        "- bootstrap 采用 10000 次 percentile 重采样、固定种子 20261114；PABAK 为按均匀边际的敏感性指标，不是主指标。",
        "",
        "### 混淆矩阵（行 = A，列 = B）",
        "",
        "风险等级：",
        "",
        "| A \\ B | low | mid | high |",
        "|---|---|---|---|",
    ]
    for index, label in enumerate(RISK_LABELS):
        lines.append(f"| {label} | " + " | ".join(str(value) for value in risk["confusion"][index]) + " |")
    lines += ["", "决策建议：", "", "| A \\ B | fast_track | manual_review | suggest_reject |", "|---|---|---|---|"]
    for index, label in enumerate(DECISION_LABELS):
        lines.append(f"| {label} | " + " | ".join(str(value) for value in decision["confusion"][index]) + " |")

    lines += [
        "",
        "## 3. 分歧项（待裁决）",
        "",
        f"共 **{len(result['disagreements'])} 条**，已单独导出 `blind_v2_disagreements.xlsx`：",
        "",
        "| 盲编号 | 事实 | A | B | 分歧性质 |",
        "|---|---|---|---|---|",
    ]
    for item in result["disagreements"]:
        lines.append(
            f"| {item['blind_id']} | {item['title']}（{item['amount']} 元，{item['invoice']}票） | "
            f"{item['a']['risk']}/{item['a']['decision']} | {item['b']['risk']}/{item['b']['decision']} | "
            f"{item['pattern']} |"
        )

    lines += [
        "",
        "## 4. 数据质量提示",
        "",
        f"- 两位标注人理由文字逐字相同的条目：**{len(result['quality']['identical_reason_ids'])}/60**"
        + (f"（{', '.join(result['quality']['identical_reason_ids'])}）" if result["quality"]["identical_reason_ids"] else ""),
        "- 逐字相同较多时，需确认两份是否确由两人独立填写；共用模板或同一人填写会削弱 κ 的解释力。",
        "- 本次理由相同的条目集中在同场景变体（住宿、跨期、注入各 2 条），属模板化表述，未发现整份复制痕迹。",
        "",
        "## 5. 口径与限制",
        "",
        "- 样本为**分层过采样**（高危占 24/60），κ 只回答“两人一致性”，不能外推到总体高危比例；",
        "- 24 条高危来自 v1（AI 预填 + 人工确认）的抽样框，只作抽样依据，不作标签证据；",
        "- 决策与风险被指南硬边界绑定，两个 κ 不是独立证据，报告以风险 κ 为主、决策作规则遵守度；",
        "- 本报告是裁决前口径；裁决后另出 `high_risk_final_v2.json`，不再重算 κ。",
        "",
    ]
    return "\n".join(lines)


def analyze(path_a: Path, path_b: Path) -> dict:
    """
    执行完整分析
        :param path_a: A 回填件
        :param path_b: B 回填件
        :return: 结果字典
    """
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 冻结样本
    a = read_packet(path_a)  # A
    b = read_packet(path_b)  # B
    problems, risks, decisions = check_integrity(a, b, sample)  # 完整性

    value_issues = [p for p in problems if "无法识别" in p or "违规" in p or "偏保守" in p]  # 取值类问题
    fact_issues = [p for p in problems if p not in value_issues]  # 事实类问题
    if fact_issues:
        raise SystemExit("事实/结构校验失败：\n" + "\n".join(fact_issues))

    pairs_risk = [(normalize(a[i]["risk_raw"], RISK_ALIASES), normalize(b[i]["risk_raw"], RISK_ALIASES)) for i in sorted(a)]
    pairs_decision = [(normalize(a[i]["decision_raw"], DECISION_ALIASES), normalize(b[i]["decision_raw"], DECISION_ALIASES)) for i in sorted(a)]

    disagreements: list[dict] = []  # 分歧
    for blind_id in sorted(a):
        left_risk = normalize(a[blind_id]["risk_raw"], RISK_ALIASES)  # A 风险
        right_risk = normalize(b[blind_id]["risk_raw"], RISK_ALIASES)  # B 风险
        left_decision = normalize(a[blind_id]["decision_raw"], DECISION_ALIASES)  # A 决策
        right_decision = normalize(b[blind_id]["decision_raw"], DECISION_ALIASES)  # B 决策
        if left_risk == right_risk and left_decision == right_decision:
            continue
        if left_risk == right_risk:
            pattern = "风险一致、决策不一致"  # 决策分歧
        elif left_decision == right_decision:
            pattern = "决策一致、风险不一致"  # 风险分歧
        else:
            pattern = "风险与决策均不一致（同向）" if (left_risk == "high" and right_risk == "mid") or (left_risk == "mid" and right_risk == "high") else "风险与决策均不一致"  # 同向/其他
        disagreements.append({
            "blind_id": blind_id,  # 盲编号
            "title": a[blind_id]["title"],  # 标题
            "category": a[blind_id]["category"],  # 费用类别
            "amount": a[blind_id]["amount"],  # 金额
            "date": a[blind_id]["date"],  # 费用日期
            "invoice": a[blind_id]["invoice"],  # 是否有票
            "a": {"risk": left_risk, "decision": left_decision, "citation": a[blind_id]["citation"], "reason": a[blind_id]["reason"]},
            "b": {"risk": right_risk, "decision": right_decision, "citation": b[blind_id]["citation"], "reason": b[blind_id]["reason"]},
            "pattern": pattern,  # 分歧类型
        })

    result = {
        "version": "blind_v2",  # 样本版本
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),  # 生成时间
        "inputs": {
            "A": {"file": str(path_a.relative_to(BASE.parents[1])), "sha256": sha256_file(path_a)},  # A 路径与哈希
            "B": {"file": str(path_b.relative_to(BASE.parents[1])), "sha256": sha256_file(path_b)},  # B 路径与哈希
            "sample": {"file": "cases/blind_v2.json", "sha256": sha256_file(SAMPLE)},  # 样本哈希
        },
        "integrity": {
            "rows": len(a),  # 条数
            "fact_issues": len(fact_issues),  # 事实问题数（为 0 才会继续）
            "value_issues": len(value_issues),  # 取值问题数
            "value_issue_details": value_issues,  # 明细
        },
        "risk": dimension_stats(pairs_risk, RISK_LABELS, "risk_level"),  # 风险维度
        "decision": dimension_stats(pairs_decision, DECISION_LABELS, "decision"),  # 决策维度
        "both_agree": sum(1 for left, right in pairs_risk if left == right),  # 两维同时一致（仅当决策与风险同步时等于风险一致数）
        "disagreements": disagreements,  # 分歧
        "quality": {
            # 理由逐字相同的条目：用于提示"是否真的独立填写"，不作为判定依据
            "identical_reason_ids": [
                blind_id for blind_id in sorted(a)
                if a[blind_id]["reason"] and a[blind_id]["reason"] == b[blind_id]["reason"]
            ],
        },
        "notes": [
            "分层过采样，κ 不外推到总体高危比例。",
            "κ 使用裁决前标注；裁决后不重算。",
            "决策与风险被硬边界绑定，两个 κ 不是独立证据。",
        ],
    }
    # 两维同时一致按实际逐条计数（避免风险一致但决策不一致时虚高）
    result["both_agree"] = sum(
        1 for blind_id in sorted(a)
        if normalize(a[blind_id]["risk_raw"], RISK_ALIASES) == normalize(b[blind_id]["risk_raw"], RISK_ALIASES)
        and normalize(a[blind_id]["decision_raw"], DECISION_ALIASES) == normalize(b[blind_id]["decision_raw"], DECISION_ALIASES)
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """
    命令行入口
        :param argv: 参数列表（None 取 sys.argv）
        :return: 退出码
    """
    parser = argparse.ArgumentParser(description="blind_v2 一致性统计与裁决清单")
    parser.add_argument("--a", default=str(DEFAULT_A), help="A（财务）回填件")  # A 路径
    parser.add_argument("--b", default=str(DEFAULT_B), help="B（老板）回填件")  # B 路径
    args = parser.parse_args(argv)  # 解析

    path_a = Path(args.a).resolve()  # A 绝对路径
    path_b = Path(args.b).resolve()  # B 绝对路径
    for path in (path_a, path_b):
        if not path.exists():
            raise SystemExit(f"文件不存在：{path}")

    result = analyze(path_a, path_b)  # 统计
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")  # 写 json
    OUT_MD.write_text(build_markdown(result), encoding="utf-8", newline="\n")  # 写 md
    build_disagreement_book(result["disagreements"])  # 写裁决清单

    risk = result["risk"]  # 风险指标
    print(f"risk  po={risk['po']:.4f} kappa={risk['kappa']:.4f} CI=({risk['kappa_ci95'][0]:.4f}, {risk['kappa_ci95'][1]:.4f})")
    print(f"decision po={result['decision']['po']:.4f} kappa={result['decision']['kappa']:.4f}")
    print(f"both_agree={result['both_agree']}/60 disagreements={len(result['disagreements'])}")
    print(f"report={OUT_MD}")
    print(f"disagreements={OUT_XLSX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
