"""
制度检索后端自检脚本（上线/排障用）
    用途：
        - 启动服务前确认 chromadb、本地 ONNX 模型、向量集合是否可用；
        - 配置了 chroma/hybrid 但实际回退 BM25 时返回非 0 退出码，便于发布流水线拦截。
    输出：
        configured / backend / clauses / degraded / elapsed_ms / error
    用法（在 backend 目录下）：
        python scripts/check_policy_backend.py
"""
import json
import sys
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.health import check_policy_backend   # noqa: E402



# 主流程
def main() -> int:
    """
    执行自检并打印结果
        :return: 退出码：0 正常（或已按配置降级），1 异常/降级
    """
    result = check_policy_backend()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["degraded"] and not result["error"] else 1



if __name__ == "__main__":
    raise SystemExit(main())
