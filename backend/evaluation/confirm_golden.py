"""
确认金标准集（P0 #1：财务/业务确认 100 条标签）
    动作：
        - 把 golden_v1.json 的 label_status 从 draft 改为 reviewed；
        - review_status 从 pending_finance 改为 confirmed；
        - 记录确认日期与确认来源，并标记 frozen=true。
    说明：
        - 本脚本只改状态，不改 expected 标签内容；
        - 确认后该文件作为冻结 test 集使用，后续变更需记录版本。
"""
import json
from datetime import date
from pathlib import Path



CASES_FILE = Path(__file__).resolve().parent / "cases" / "golden_v1.json"



def main() -> int:
    """
    更新 100 条案例的确认状态
        :return: 退出码
    """
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    confirmed_at = date.today().isoformat()
    for case in cases:
        case["label_status"] = "reviewed"
        case["review_status"] = "confirmed"
        case["reviewed_at"] = confirmed_at
        case["reviewed_by"] = "business"
        case["frozen"] = True
        case["review_note"] = f"用户确认 100 条标签无误（{confirmed_at}）"
    CASES_FILE.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"confirmed={len(cases)} path={CASES_FILE} at={confirmed_at}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
