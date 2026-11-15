"""
生成 blind_v2 两份盲包（P0-1 第②步）

    做什么：
        1. 读取已冻结的 blind_v2.json（60 条事实），先校验哈希，防止拿被改过的样本发包；
        2. 生成 A（财务）/ B（老板）两份 xlsx：内容相同、**顺序各自独立打乱**；
        3. 工作簿三页：标注表（60 行事实 + 待填列）、标注说明、制度条款（33 条原文）；
        4. 记录发放清单 blind_v2_distribution.json：指南哈希、样本哈希、两份包哈希、检查项；
        5. --verify 重新校验哈希、行数、编号集合、顺序差异与泄题。

    边界：
        - 包内不含 AI 结论、规则命中、工程标签、对方答案、案例原始编号与分层；
        - 待填列（风险/决策/引用/理由/备注）保持空白，标注人自行填写；
        - 包本身不设只读（标注人需要回填），其"发放时哈希"记在发放清单里。

    用法（在 backend/ 下）：
        python evaluation/build_blind_v2_packets.py
        python evaluation/build_blind_v2_packets.py --verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import stat
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


# 本脚本所在目录（backend/evaluation）
BASE = Path(__file__).resolve().parent
# 案例目录
CASES_DIR = BASE / "cases"
# 输出目录（工作簿放这里，与历史复核表同目录）
REPORTS_DIR = BASE / "reports"
# 冻结样本
SAMPLE = CASES_DIR / "blind_v2.json"
# 冻结样本的哈希清单
SAMPLE_HASH = CASES_DIR / "blind_v2.sha256"
# 发放清单（本脚本产物）
DIST = CASES_DIR / "blind_v2_distribution.json"
# 标注指南（必须先定稿）
GUIDE = BASE.parents[1] / "docs" / "annotate_guide_v2.0.md"
# 制度原文（条款页从这里解析，保证与制度一字不差）
POLICY = BASE.parents[1] / "docs" / "policies" / "expense_policy_v1.md"

# 两位标注人：角色与输出文件
PACKETS: dict[str, dict] = {
    "A": {"role": "复核人 A：财务", "file": REPORTS_DIR / "blind_v2_packet_A.xlsx"},
    "B": {"role": "复核人 B：老板", "file": REPORTS_DIR / "blind_v2_packet_B.xlsx"},
}

# 标注表列：前 8 列是事实，后 5 列待填
HEADERS = (
    "盲编号", "标题/费用说明", "费用类别", "金额(元)", "费用日期",
    "提交时间", "是否有票", "无票说明",
    "风险等级", "决策建议", "引用条款(可选)", "理由", "备注",
)
# 待填列起始列号（I=9）
FILL_FIRST_COL = 9
# 数据起始行（第 4 行表头）
DATA_FIRST_ROW = 5
# 解析 policy:clause 注释块
CLAUSE_RE = re.compile(
    r"<!--\s*policy:clause\s+(?P<meta>.*?)-->\s*(?P<text>.*?)\s*<!--\s*/policy:clause\s*-->",
    re.S,
)

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


def read_hash_manifest() -> dict[str, str]:
    """
    读取 blind_v2.sha256（sha256sum 格式）
        :return: 文件名 → 哈希
    """
    expected: dict[str, str] = {}  # 文件名 → 哈希
    for line in SAMPLE_HASH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)  # 拆分 "hash  file"
            expected[name.strip()] = digest.strip()
    return expected


def load_sample() -> dict:
    """
    校验并读取冻结样本（哈希不符直接拒绝发包）
        :return: blind_v2.json 内容
    """
    if not SAMPLE.exists():
        raise SystemExit("blind_v2.json 不存在，请先执行 build_blind_v2.py")
    expected = read_hash_manifest()  # 哈希清单
    if expected.get(SAMPLE.name) != sha256_file(SAMPLE):
        raise SystemExit("blind_v2.json 哈希与清单不符，样本已被改动，拒绝发包")
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 样本
    if payload["meta"]["status"] != "frozen" or payload["meta"]["n"] != 60:
        raise SystemExit("样本未冻结或条数不是 60")
    return payload


def load_clauses() -> list[dict]:
    """
    从制度原文解析 33 条条款（原文照抄，不做改写）
        :return: [{"clause_id","applies_to","text"}]
    """
    text = POLICY.read_text(encoding="utf-8")  # 制度全文
    clauses: list[dict] = []  # 条款列表
    for match in CLAUSE_RE.finditer(text):
        meta_lines = match.group("meta").strip().splitlines()  # 注释块里的元数据行
        meta: dict[str, str] = {}  # 元数据
        for line in meta_lines:
            if ":" in line:
                key, value = line.split(":", 1)  # 只切第一个冒号
                meta[key.strip()] = value.strip()
        body = " ".join(match.group("text").split())  # 正文压成一行
        clauses.append({
            "clause_id": meta.get("clause_id", ""),  # 条款号
            "applies_to": meta.get("applies_to", ""),  # 适用范围
            "text": body,  # 条款原文
        })
    if len(clauses) != 33:
        raise SystemExit(f"制度条款应为 33 条，实际 {len(clauses)} 条")
    return clauses


def case_cell(title: str, items: list[dict]) -> str:
    """
    组装"标题/费用说明"单元格：说明与标题不同时一并展示，避免丢事实
        :param title: 标题
        :param items: 明细列表
        :return: 展示文本
    """
    descriptions = []  # 明细说明
    for item in items:
        desc = (item.get("description") or "").strip()  # 单条说明
        if desc and desc != title and desc not in descriptions:
            descriptions.append(desc)
    return title if not descriptions else f"{title}｜{'；'.join(descriptions)}"


def write_cell(ws, row: int, col: int, value, *, fill=None, center=False, bold=False, wrap=True):
    """
    写单元格并套样式
        :param ws: 工作表
        :param row: 行号
        :param col: 列号
        :param value: 值
        :param fill: 填充色
        :param center: 是否居中
        :param bold: 是否加粗
        :param wrap: 是否自动换行
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


def build_sheet(ws, rows: list[dict], role: str):
    """
    构建"标注表"页
        :param ws: 工作表
        :param rows: 已乱序的 60 条事实
        :param role: 标注人角色说明
    """
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    title = ws.cell(row=1, column=1, value=f"blind_v2 独立标注表 — {role}")
    title.font = TITLE_FONT
    title.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(HEADERS))
    note = ws.cell(
        row=2, column=1,
        value="只依据本页单据事实与《制度条款》页判断，不要参考 AI 结论、规则命中或他人意见。"
              "风险=low/mid/high；决策=fast_track/manual_review/suggest_reject；引用条款可选。",
    )
    note.font = Font(name="Microsoft YaHei", size=9, color="595959")
    note.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 34

    for index, text in enumerate(HEADERS, start=1):
        cell = ws.cell(row=4, column=index, value=text)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 32

    for row_index, row in enumerate(rows, start=DATA_FIRST_ROW):
        items = row["items"]  # 明细
        if len(items) != 1:
            raise SystemExit(f"{row['blind_id']} 明细数不是 1，盲包列结构需重新设计")
        item = items[0]  # 唯一明细
        values = (
            row["blind_id"],
            case_cell(row["title"], items),
            item.get("expense_type_label"),
            row["total_amount"],
            item.get("expense_date"),
            row.get("submitted_at"),
            "有" if item.get("has_invoice") else "无",
            item.get("no_invoice_reason") or "—",
            None,  # 风险等级（待填）
            None,  # 决策建议（待填）
            None,  # 引用条款（待填）
            None,  # 理由（待填）
            None,  # 备注（待填）
        )
        for col, value in enumerate(values, start=1):
            write_cell(
                ws, row_index, col, value,
                fill=REVIEW_FILL if col >= FILL_FIRST_COL else None,  # 待填列高亮
                center=col in (1, 3, 4, 5, 7),
            )
        ws.row_dimensions[row_index].height = 44

    widths = (10, 34, 11, 10, 12, 18, 9, 18, 11, 17, 15, 38, 22)  # 列宽
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:M{4 + len(rows)}"
    ws.sheet_view.showGridLines = False

    # 风险/决策下拉，减少手填错值（错值会让 κ 计算直接失败）
    risk_dv = DataValidation(type="list", formula1='"low,mid,high"', allow_blank=True)
    decision_dv = DataValidation(
        type="list",
        formula1='"fast_track,manual_review,suggest_reject"',
        allow_blank=True,
    )
    ws.add_data_validation(risk_dv)
    ws.add_data_validation(decision_dv)
    last_row = DATA_FIRST_ROW + len(rows) - 1  # 64
    risk_dv.add(f"I{DATA_FIRST_ROW}:I{last_row}")
    decision_dv.add(f"J{DATA_FIRST_ROW}:J{last_row}")


def build_guide(ws, role: str):
    """
    构建"标注说明"页（指南压缩版）
        :param ws: 工作表
        :param role: 标注人角色
    """
    ws.merge_cells("A1:B1")
    title = ws.cell(row=1, column=1, value="标注说明（完整版见 docs/annotate_guide_v2.0.md）")
    title.font = TITLE_FONT
    ws.row_dimensions[1].height = 28

    rows = [
        ("标注人", role),
        ("样本", "60 条脱敏单据，含 1 条明细；盲编号仅用于合并结果，与风险高低无关"),
        ("独立性", "不看对方表格、不讨论、不参考系统 AI 结论；同一时间窗内独立完成，只填不回改"),
        ("风险 low", "事实齐全、票证合规、金额在限额内，无违规特征"),
        ("风险 mid", "有需核实的疑点：缺票/弱凭证、超限、跨期、金额/抬头/发票号不一致、审批缺失、识别失败"),
        ("风险 high", "明确违规或舞弊：虚报、重复报销、个人消费、测试单据、抬头非公司、拆票规避权限"),
        ("决策 fast_track", "低风险且无中高风险疑点；只是建议正常审批，不等于自动通过"),
        ("决策 manual_review", "有疑点需人工核实（默认档）；风险为 mid 时必须选这一档"),
        ("决策 suggest_reject", "明确违规，建议驳回；风险为 high 时通常选这一档"),
        ("引用条款", "填条款号（如 2.4），多条用逗号分隔；不确定留空，不得编造"),
        ("理由", "必填，一句话写清依据（金额、票号、日期、条款号等），便于分歧裁决"),
        ("交付", "另存为 blind_v2_packet_A_<姓名>_<日期>.xlsx 交回；分歧由第三人裁决"),
    ]
    for index, (name, text) in enumerate(rows, start=3):
        ws.cell(row=index, column=1, value=name).font = BOLD_FONT
        ws.cell(row=index, column=1).fill = GRAY_FILL
        ws.cell(row=index, column=1).border = BORDER
        ws.cell(row=index, column=1).alignment = Alignment(vertical="center", wrap_text=True)
        ws.cell(row=index, column=2, value=text).font = BODY_FONT
        ws.cell(row=index, column=2).border = BORDER
        ws.cell(row=index, column=2).alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[index].height = 34
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100
    ws.sheet_view.showGridLines = False


def build_clauses(ws, clauses: list[dict]):
    """
    构建"制度条款"页（33 条原文，供引用时查阅）
        :param ws: 工作表
        :param clauses: 条款列表
    """
    headers = ("条款号", "适用范围", "原文")
    for index, text in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=index, value=text)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_index, clause in enumerate(clauses, start=2):
        values = (clause["clause_id"], clause["applies_to"], clause["text"])
        for col, value in enumerate(values, start=1):
            write_cell(ws, row_index, col, value, center=col == 1)
        ws.row_dimensions[row_index].height = 46

    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 110
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def build_packet(rows: list[dict], annotator: str, clauses: list[dict]) -> Path:
    """
    生成一份盲包并保存
        :param rows: 已按该标注人顺序打乱的 60 条
        :param annotator: "A" / "B"
        :param clauses: 33 条制度条款
        :return: 输出文件路径
    """
    meta = PACKETS[annotator]  # 该标注人的角色与输出
    wb = Workbook()  # 新工作簿
    wb.active.title = "标注表"
    build_sheet(wb.active, rows, meta["role"])
    build_guide(wb.create_sheet("标注说明"), meta["role"])
    build_clauses(wb.create_sheet("制度条款"), clauses)
    wb.properties.title = f"blind_v2 独立标注 - {meta['role']}"
    wb.properties.creator = "expense-audit-system"
    output = meta["file"]  # 输出路径
    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    return output


def scan_packet(path: Path) -> tuple[bool, list[str]]:
    """
    重新打开盲包做泄题与空白检查
        :param path: 工作簿路径
        :return: (是否通过, 问题列表)
    """
    problems: list[str] = []  # 问题
    wb = load_workbook(path)  # 只读打开
    if wb.sheetnames != ["标注表", "标注说明", "制度条款"]:
        problems.append(f"工作表不符：{wb.sheetnames}")

    sheet = wb["标注表"]  # 标注表
    if sheet.max_row != DATA_FIRST_ROW + 59:  # 64
        problems.append(f"标注表行数不符：{sheet.max_row}")

    # 1) 待填列必须为空
    for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60):
        for col in range(FILL_FIRST_COL, FILL_FIRST_COL + 5):
            if sheet.cell(row=row, column=col).value is not None:
                problems.append(f"第 {row} 行待填列 {col} 非空")
                break

    # 2) 不得出现案例原始编号（G0xx）
    pattern = re.compile(r"\bG\d{3}\b")
    for ws in (sheet, wb["制度条款"]):
        for row in ws.iter_rows(values_only=True):
            for value in row:
                if isinstance(value, str) and pattern.search(value):
                    problems.append(f"{ws.title} 含 G 编号：{value[:40]}")
                    break

    # 3) 条款页 33 条
    if wb["制度条款"].max_row != 34:
        problems.append(f"条款页行数不符：{wb['制度条款'].max_row}")
    return not problems, problems


def build() -> int:
    """
    生成两份盲包与发放清单
        :return: 退出码
    """
    if not GUIDE.exists():
        raise SystemExit("标注指南不存在，请先定稿 docs/annotate_guide_v2.0.md")
    payload = load_sample()  # 校验并读取样本
    clauses = load_clauses()  # 33 条原文
    seed = payload["meta"]["seed"]  # 抽样种子（用于派生顺序种子）

    packet_info: dict[str, dict] = {}  # 发放信息
    orders: dict[str, list[str]] = {}  # 两份包的顺序
    for annotator, meta in PACKETS.items():
        # 每份包独立的乱序种子；同一份包重跑结果一致，A/B 之间顺序不同
        rng = random.Random(f"{seed}:{annotator}")
        rows = list(payload["cases"])  # 复制，避免互相影响
        rng.shuffle(rows)  # 打乱
        orders[annotator] = [row["blind_id"] for row in rows]  # 记录顺序

        output = build_packet(rows, annotator, clauses)  # 生成
        ok, problems = scan_packet(output)  # 自检
        if not ok:
            raise SystemExit(f"{annotator} 包自检失败：{problems}")

        packet_info[annotator] = {
            "role": meta["role"],  # 角色
            "file": str(output.relative_to(BASE.parents[1])),  # 仓库内相对路径
            "sha256": sha256_file(output),  # 发放时哈希
            "rows": len(rows),  # 条数
        }

    if orders["A"] == orders["B"]:
        raise SystemExit("A/B 两份包顺序相同，独立乱序失败")

    manifest = {
        "version": "blind_v2",  # 版本
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),  # 生成时间
        "guide": {
            "file": "docs/annotate_guide_v2.0.md",  # 指南路径
            "version": "annotate_guide_v2.0",  # 指南版本
            "sha256": sha256_file(GUIDE),  # 指南哈希（发放前锁定）
        },
        "sample": {
            "file": "cases/blind_v2.json",  # 样本路径
            "sha256": sha256_file(SAMPLE),  # 样本哈希
            "n": payload["meta"]["n"],  # 60
        },
        "policy": {
            "file": "docs/policies/expense_policy_v1.md",  # 制度路径
            "sha256": sha256_file(POLICY),  # 制度哈希
            "clauses": len(clauses),  # 33
        },
        "packets": packet_info,  # 两份包
        "checks": {
            "orders_differ": True,  # 顺序不同
            "answer_cells_empty": True,  # 待填列为空
            "leak_scan": "pass",  # 无 G 编号/无标签
            "clauses": len(clauses),  # 33 条
        },
        "note": "记录的是发包时的空白版本哈希；标注人回填后哈希会变，属正常。",
    }

    if DIST.exists():  # 已有清单时先解冻再覆盖
        os.chmod(DIST, stat.S_IWRITE | stat.S_IREAD)
    DIST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.chmod(DIST, stat.S_IREAD)  # 清单只读
    os.chmod(GUIDE, stat.S_IREAD)  # 指南只读，发放后不得改

    for annotator, info in packet_info.items():
        print(f"packet {annotator} rows={info['rows']} sha256={info['sha256'][:16]}… file={info['file']}")
    print(f"guide sha256={manifest['guide']['sha256'][:16]}…")
    print(f"manifest={DIST}")
    return 0


def verify() -> int:
    """
    校验发放清单、指南哈希、两份包内容与泄题
        :return: 0 通过；1 失败
    """
    failures: list[str] = []  # 失败项
    checks: list[str] = []  # 通过项

    if not DIST.exists():
        print("FAIL 发放清单不存在，请先执行生成")
        return 1
    manifest = json.loads(DIST.read_text(encoding="utf-8"))  # 清单

    # 1) 指南与制度哈希
    if sha256_file(GUIDE) == manifest["guide"]["sha256"]:
        checks.append("指南哈希一致")
    else:
        failures.append("指南已被修改，盲包口径失效")
    if sha256_file(POLICY) == manifest["policy"]["sha256"]:
        checks.append("制度原文哈希一致")
    else:
        failures.append("制度原文已被修改")
    if sha256_file(SAMPLE) == manifest["sample"]["sha256"]:
        checks.append("冻结样本哈希一致")
    else:
        failures.append("冻结样本已被修改")

    # 2) 每份包：哈希、行数、编号集合、顺序差异、泄题
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 样本
    sample_ids = {row["blind_id"] for row in sample["cases"]}  # 60 个盲编号
    orders: dict[str, list[str]] = {}  # 顺序
    for annotator, info in manifest["packets"].items():
        path = BASE.parents[1] / info["file"]  # 仓库内路径转绝对
        if not path.exists():
            failures.append(f"{annotator} 包不存在")
            continue
        if sha256_file(path) != info["sha256"]:
            failures.append(f"{annotator} 包哈希不一致（发放后被改过）")
            continue
        checks.append(f"{annotator} 包哈希一致")

        wb = load_workbook(path)  # 打开
        sheet = wb["标注表"]  # 标注表
        ids = [sheet.cell(row=row, column=1).value for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60)]  # 本包顺序
        orders[annotator] = ids
        if set(ids) == sample_ids and len(set(ids)) == 60:
            checks.append(f"{annotator} 包编号集合一致")
        else:
            failures.append(f"{annotator} 包编号集合不一致")

        ok, problems = scan_packet(path)  # 内容自检
        if ok:
            checks.append(f"{annotator} 包内容/泄题检查通过")
        else:
            failures.extend(problems)

    if len(orders) == 2:
        if orders["A"] != orders["B"]:
            checks.append("A/B 顺序不同")
        else:
            failures.append("A/B 顺序相同，独立乱序失效")

    for line in checks:
        print(f"OK   {line}")
    for line in failures:
        print(f"FAIL {line}")
    print(f"RESULT checks={len(checks)} failures={len(failures)}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    """
    命令行入口
        :param argv: 参数列表（None 取 sys.argv）
        :return: 退出码
    """
    parser = argparse.ArgumentParser(description="生成/校验 blind_v2 两份盲包")
    parser.add_argument("--verify", action="store_true", help="只校验，不写盘")  # 校验模式
    args = parser.parse_args(argv)  # 解析参数

    if args.verify:
        return verify()
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
