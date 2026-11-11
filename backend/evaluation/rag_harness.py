"""
Phase D 检索评测（BM25 基线）
    目的：
        - 用固定查询集计算 Recall@1/3/5 与 MRR；
        - 如果 Recall@3 低于阈值（建议 85%），再评估是否引入 Chroma + Embedding；
        - 当前不依赖 Chroma，确保基线可复现。
"""
import json
from decimal import Decimal
from pathlib import Path

from app.rag.loader import load_policy_document
from app.rag.retriever import BM25PolicyRetriever
from app.rag.schema import PolicyQuery



# 默认查询集
DEFAULT_QUERIES = Path(__file__).resolve().parent / "cases" / "rag_queries.json"

# 口语化压测集：员工说法与制度用词不一致，用来判断 BM25 的字面匹配缺口
COLLOQUIAL_QUERIES = Path(__file__).resolve().parent / "cases" / "rag_queries_colloquial.json"

# Recall@3 建议门槛
RECALL_GATE = 0.85



# 读取查询集
def load_queries(path: str | Path = DEFAULT_QUERIES) -> list[dict]:
    """
    读取检索评测查询
        :param path: 查询文件
        :return: 查询列表
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))



# 执行检索评测
def evaluate_retrieval(
    path: str | Path = DEFAULT_QUERIES,
    *,
    top_k: int = 5,
    backend: str = "bm25",
) -> dict:
    """
    执行检索评测
        :param path: 查询文件
        :param top_k: 最大截断
        :param backend: 检索后端：bm25 / chroma
        :return: 指标与逐条结果
    """
    queries = load_queries(path)
    document = load_policy_document()

    # 1.按后端创建检索器（chroma/hybrid 延迟导入，未安装时直接报错，便于评测脚本提示）
    if backend == "chroma":
        from app.rag.chroma_retriever import ChromaPolicyRetriever

        retriever = ChromaPolicyRetriever(document)
    elif backend == "hybrid":
        from app.rag.hybrid_retriever import HybridPolicyRetriever

        retriever = HybridPolicyRetriever(document)
    else:
        retriever = BM25PolicyRetriever(document)

    results = []
    reciprocal_ranks = []
    hits_at = {1: 0, 3: 0, 5: 0}

    for item in queries:
        query = PolicyQuery(
            text=str(item.get("query") or ""),
            expense_types=frozenset(item.get("expense_types") or []),
            role=str(item.get("role") or ""),
            total_amount=Decimal(str(item.get("amount") or "0")),
        )
        hits = retriever.retrieve(query)[:top_k]
        hit_ids = [hit.clause.clause_id for hit in hits]
        expected = list(item.get("expected") or [])

        # 找到第一个命中的期望条款
        rank = None
        for index, clause_id in enumerate(hit_ids, start=1):
            if clause_id in expected:
                rank = index
                break

        for k in hits_at:
            if rank is not None and rank <= k:
                hits_at[k] += 1
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        results.append({
            "query_id": item.get("query_id"),
            "query": item.get("query"),
            "expected": expected,
            "hits": hit_ids,
            "first_rank": rank,
            "hit": rank is not None,
        })

    total = len(results)
    return {
        "mode": backend,
        "total": total,
        "recall_at_1": round(hits_at[1] / total, 4) if total else 0.0,
        "recall_at_3": round(hits_at[3] / total, 4) if total else 0.0,
        "recall_at_5": round(hits_at[5] / total, 4) if total else 0.0,
        "mrr": round(sum(reciprocal_ranks) / total, 4) if total else 0.0,
        "recall_gate": RECALL_GATE,
        "need_chroma": (hits_at[3] / total) < RECALL_GATE if total else False,
        "results": results,
    }



# Markdown 报告
def render_rag_markdown(report: dict) -> str:
    """
    渲染检索评测 Markdown
        :param report: evaluate_retrieval 结果
        :return: Markdown
    """
    lines = [
        f"# 制度检索评测（{report.get('mode', 'bm25')} 后端）",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| 查询数 | {report['total']} |",
        f"| Recall@1 | {report['recall_at_1']:.2%} |",
        f"| Recall@3 | {report['recall_at_3']:.2%} |",
        f"| Recall@5 | {report['recall_at_5']:.2%} |",
        f"| MRR | {report['mrr']:.4f} |",
        f"| Recall@3 门槛 | {report['recall_gate']:.2%} |",
        f"| 是否需要 Chroma | {'是' if report['need_chroma'] else '否'} |",
        "",
        "## 未命中查询",
        "",
        "| 查询 | 期望条款 | Top-5 |",
        "|---|---|---|",
    ]
    for item in report["results"]:
        if item["hit"]:
            continue
        lines.append(
            f"| {item['query']} | {', '.join(item['expected'])} | {', '.join(item['hits']) or '-'} |"
        )
    return "\n".join(lines) + "\n"
