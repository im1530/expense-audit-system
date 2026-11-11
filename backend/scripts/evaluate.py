"""
Phase F 评测入口
    模式：
        - mock（默认）：读取案例里的 simulated 结果，验证决策层与指标；
        - real：调用真实 DeepSeek 风险 Agent，录制结果并评测（会产生费用）；
        - replay：读取录制文件，不调用模型，复现历史结果。
    用法（在 backend 目录下）：
        python scripts/evaluate.py
        python scripts/evaluate.py --cases evaluation/cases/golden_v1.json
        python scripts/evaluate.py --mode replay --recording evaluation/recordings/real_xxx.json
        python scripts/evaluate.py --mode real --limit 5 --recording evaluation/recordings/preview.json
    说明：
        - real 模式必须先取得授权并确认费用；
        - 录制文件只保存结构化结论/token/耗时，不保存模型原始返回；
        - 金标准集标签仍为 draft，需财务/业务复核。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 允许从 backend 目录导入 evaluation/app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.risk_agent import RiskAgent
from app.agents.prompts import PROMPT_VERSION_V3, SYSTEM_PROMPT_V3
from app.agents.prompts_v4 import PROMPT_VERSION as PROMPT_VERSION_V4, SYSTEM_PROMPT_V4
from app.config import settings
from evaluation.harness import DEFAULT_CASES, evaluate_cases, load_cases, render_markdown
from evaluation.runner import (
    RECORDING_DIR,
    load_recording,
    outcomes_from_recording,
    record_cases,
    save_recording,
)



# 默认输出
DEFAULT_JSON = Path("evaluation/reports/latest.json")
DEFAULT_MD = Path("evaluation/reports/latest.md")



# 百分比格式化
def _pct(value: float | None) -> str:
    """
    把比率格式化成百分比文本
        :param value: 0~1 的比率；None 表示不适用
        :return: 形如 90.91% 或 n/a
    """
    return "n/a" if value is None else f"{value:.2%}"



# 解析参数
def parse_args():
    """
    解析命令行参数
        :return: argparse.Namespace
    """
    parser = argparse.ArgumentParser(description="Phase F 评测")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="案例 JSON 路径")
    parser.add_argument("--mode", choices=("mock", "real", "replay"), default="mock", help="评测模式")
    parser.add_argument("--output", default=str(DEFAULT_JSON), help="JSON 报告输出路径")
    parser.add_argument("--markdown", default=str(DEFAULT_MD), help="Markdown 报告输出路径")
    parser.add_argument("--recording", default="", help="real 输出 / replay 输入的录制文件")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 表示全部）")
    parser.add_argument("--case-ids", default="", help="逗号分隔的 case_id，只跑指定案例")
    parser.add_argument("--prompt", choices=("v3", "v4"), default="v4", help="real 模式使用的 Prompt 版本")
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    执行评测并写报告
        :return: 退出码：0 成功，2 配置/输入错误
    """
    args = parse_args()
    cases = load_cases(args.cases)
    if args.case_ids:
        wanted = {item.strip() for item in args.case_ids.split(",") if item.strip()}
        cases = [case for case in cases if case.get("case_id") in wanted]
    if args.limit > 0:
        cases = cases[: args.limit]

    # 1.mock：使用 simulated 字段
    if args.mode == "mock":
        report = evaluate_cases(cases)
        recording_meta = {}

    # 2.real：调用真实 Agent 并录制
    elif args.mode == "real":
        if not settings.DEEPSEEK_API_KEY.get_secret_value():
            print("未配置 DEEPSEEK_API_KEY，无法执行 real 模式")
            return 2
        recording_path = Path(args.recording) if args.recording else (
            RECORDING_DIR / f"real_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        print(f"real 模式：将调用真实模型，cases={len(cases)}，会产生费用，prompt={args.prompt}")
        agent = None
        if args.prompt == "v3":
            agent = RiskAgent(
                system_prompt=SYSTEM_PROMPT_V3,
                prompt_version=PROMPT_VERSION_V3,
            )
        elif args.prompt == "v4":
            agent = RiskAgent(
                system_prompt=SYSTEM_PROMPT_V4,
                prompt_version=PROMPT_VERSION_V4,
            )
        recording = record_cases(cases, limit=None, agent=agent)
        saved = save_recording(recording, recording_path)
        outcomes = outcomes_from_recording(recording)
        report = evaluate_cases(cases, outcomes, mode="real")
        report["meta"] = recording["meta"]
        recording_meta = {"recording": str(saved)}

    # 3.replay：读取历史录制
    else:
        if not args.recording:
            print("replay 模式必须提供 --recording")
            return 2
        recording = load_recording(args.recording)
        outcomes = outcomes_from_recording(recording)
        report = evaluate_cases(cases, outcomes, mode="replay")
        report["meta"] = recording.get("meta") or {}
        recording_meta = {"recording": args.recording}

    # 4.写 JSON 与 Markdown 报告
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = Path(args.markdown)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_markdown(report), encoding="utf-8")

    print(f"mode={report['mode']} cases={report['total']}")
    print(f"decision_accuracy={report['decision_accuracy']:.2%}")
    print(f"risk_level_accuracy={report['risk_level_accuracy']:.2%}")
    print(f"high_risk_miss_rate={report['high_risk_miss_rate']:.2%}")
    print(f"fallback_rate={report['fallback_rate']:.2%}")
    print(f"finding_recall={report['finding_recall']:.2%}")
    # 引用指标：mock 模式不产生引用，只有已标注的 real/replay 报告才输出
    if report.get("citation_applicable") and report.get("citation_labeled_cases"):
        print(f"citation_labeled_cases={report['citation_labeled_cases']}")
        print(f"citation_precision={_pct(report.get('citation_precision'))}")
        print(f"citation_recall={_pct(report.get('citation_recall'))}")
        print(f"citation_retrieval_coverage={_pct(report.get('citation_retrieval_coverage'))}")
        print(f"citation_recall_retrieved={_pct(report.get('citation_recall_retrieved'))}")
    print(f"avg_latency_ms={report['avg_latency_ms']}")
    print(f"avg_tokens={report['avg_tokens']}")
    if recording_meta.get("recording"):
        print(f"recording={recording_meta['recording']}")
    print(f"report_json={output}")
    print(f"report_markdown={markdown}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
