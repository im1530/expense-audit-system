"""
把高危复核表的复核人改为数据库中的真实姓名（财务 + 老板）
    数据来源：
        users.username = fin1（财务）
        users.username = boss1（老板）
    动作：
        - M 列写“姓名（角色）”；
        - O 列写简洁的复核角色备注；
        - 标题/说明改为“财务复核 / 老板复核”。
"""
from datetime import date
from pathlib import Path
import json

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill



BASE = Path(__file__).resolve().parent
NAMES_FILE = BASE / "reports" / "reviewer_names.json"
FILES = {
    "finance": (BASE / "reports" / "high_risk_review_finance.xlsx", "fin1", "财务"),
    "boss": (BASE / "reports" / "high_risk_review_boss.xlsx", "boss1", "老板"),
}
DONE_FILL = PatternFill("solid", fgColor="E2EFDA")
BODY_FONT = Font(name="Microsoft YaHei", size=10)



def load_names() -> dict[str, str]:
    """
    读取财务/老板姓名（由项目 venv 从数据库导出）
        :return: {username: full_name}
    """
    return json.loads(NAMES_FILE.read_text(encoding="utf-8"))



def main() -> int:
    """
    更新两份复核表
        :return: 退出码
    """
    names = load_names()
    for role, (path, username, label) in FILES.items():
        reviewer = f"{names.get(username, username)}（{label}）"
        wb = load_workbook(path)
        sheet = wb["高危24条"]
        sheet["A1"] = f"高危 24 条复核表 — {label}复核"
        sheet["A2"] = f"AI 预填，复核人确认；复核角色：{label}（{username}）"

        for row in range(5, 29):
            sheet.cell(row=row, column=13, value=reviewer)
            sheet.cell(row=row, column=14, value=date.today().isoformat())
            sheet.cell(row=row, column=15, value=f"{label}复核")
            for col in (13, 14, 15):
                cell = sheet.cell(row=row, column=col)
                cell.font = BODY_FONT
                cell.fill = DONE_FILL
                cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)

        wb.save(path)
        checked = load_workbook(path)
        target = checked["高危24条"]
        assert target["M5"].value == reviewer
        assert target["O28"].value == f"{label}复核"
        print(f"updated={path} reviewer={reviewer} note={label}复核")

    return 0



if __name__ == "__main__":
    raise SystemExit(main())
