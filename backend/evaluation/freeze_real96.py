"""
冻结真实评测集（P0 #1/#2）
    背景：
        G073-G076 是 ai_fallback 模拟降级场景，真实模型无法强制触发，
        放在真实 test 集里会污染决策一致率、finding 召回率和违规数。
    动作：
        1. 在 golden_v1.json 中给 4 条 ai_fallback 标记 eval_scope=mock_only；
        2. 生成 golden_v1_real96.json，只保留 96 条可在真实模式复现的案例；
        3. 两个文件都保持 reviewed/frozen 状态。
"""
import json
from datetime import date
from pathlib import Path



BASE = Path(__file__).resolve().parent / "cases"
FULL_FILE = BASE / "golden_v1.json"
REAL_FILE = BASE / "golden_v1_real96.json"
MOCK_ONLY = {"G073", "G074", "G075", "G076"}



def main() -> int:
    """
    生成真实 96 条集
        :return: 退出码
    """
    cases = json.loads(FULL_FILE.read_text(encoding="utf-8"))
    real_cases = []
    for case in cases:
        if case["case_id"] in MOCK_ONLY:
            case["eval_scope"] = "mock_only"
            case["exclude_reason"] = "ai_fallback 模拟降级场景，真实模式无法强制触发"
        else:
            case["eval_scope"] = "real"
            real_cases.append(case)

    FULL_FILE.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    REAL_FILE.write_text(json.dumps(real_cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"full={len(cases)} real96={len(real_cases)} at={date.today().isoformat()}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
