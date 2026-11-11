"""
评测报告对比入口（Phase F：Prompt/模型版本对比）
    用法（在 backend 目录下）：
        python scripts/compare_evaluations.py \
            --baseline evaluation/reports/latest.json \
            --candidate evaluation/reports/candidate.json \
            --output evaluation/reports/compare.json \
            --markdown evaluation/reports/compare.md
    说明：
        - 两份报告必须使用同一套案例；
        - 只做纯计算，不调用模型。
"""
import argparse
import json
import sys
from pathlib import Path

# 允许从 backend 目录导入 evaluation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.compare import compare_reports, render_comparison_markdown



# 解析参数
def parse_args() -> argparse.Namespace:
    """
    解析命令行参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="对比两份评测报告")
    parser.add_argument("--baseline", required=True, help="基线报告 JSON")
    parser.add_argument("--candidate", required=True, help="候选报告 JSON")
    parser.add_argument("--output", default="evaluation/reports/compare.json", help="对比 JSON 输出")
    parser.add_argument("--markdown", default="evaluation/reports/compare.md", help="对比 Markdown 输出")
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    执行对比并写文件
        :return: 退出码
    """
    args = parse_args()
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, candidate)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = Path(args.markdown)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_comparison_markdown(comparison), encoding="utf-8")

    print(f"cases={comparison['total']} improved={comparison['improved_cases']} regressed={comparison['regressed_cases']}")
    for key, item in comparison["metrics"].items():
        print(f"{key}: baseline={item['baseline']} candidate={item['candidate']} delta={item['delta']}")
    print(f"compare_json={output}")
    print(f"compare_markdown={markdown}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
