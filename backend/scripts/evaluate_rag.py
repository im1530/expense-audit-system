"""
Phase D 检索评测入口（离线，不联网）
    用法（在 backend 目录下）：
        python scripts/evaluate_rag.py
    输出：
        evaluation/reports/rag_latest.json
        evaluation/reports/rag_latest.md
"""
import argparse
import json
import sys
from pathlib import Path

# 允许从 backend 目录导入 evaluation/app
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.rag_harness import DEFAULT_QUERIES, evaluate_retrieval, render_rag_markdown



# 解析参数
def parse_args():
    """
    解析命令行参数
        :return: argparse.Namespace
    """
    parser = argparse.ArgumentParser(description="Phase D 检索评测")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES), help="查询 JSON 路径")
    parser.add_argument("--backend", choices=("bm25", "chroma", "hybrid"), default="bm25", help="检索后端")
    parser.add_argument("--output", default="evaluation/reports/rag_latest.json", help="JSON 输出")
    parser.add_argument("--markdown", default="evaluation/reports/rag_latest.md", help="Markdown 输出")
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    执行检索评测
        :return: 退出码
    """
    args = parse_args()
    report = evaluate_retrieval(args.queries, backend=args.backend)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = Path(args.markdown)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(render_rag_markdown(report), encoding="utf-8")

    print(f"queries={report['total']}")
    print(f"recall@1={report['recall_at_1']:.2%}")
    print(f"recall@3={report['recall_at_3']:.2%}")
    print(f"recall@5={report['recall_at_5']:.2%}")
    print(f"mrr={report['mrr']:.4f}")
    print(f"need_chroma={report['need_chroma']}")
    print(f"report_json={output}")
    print(f"report_markdown={markdown}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
