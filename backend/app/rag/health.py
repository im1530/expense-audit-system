"""
制度检索启动自检（Phase D 运维）
    目的：
        - 启动时把检索后端真正初始化一次，模型/向量库有问题在启动日志里就能看到；
        - 配置了 chroma/hybrid 但实际回退 BM25 时，明确告警，避免"以为在用向量检索"；
        - 同时预热模型，避免首个预审请求承担 2~3 秒加载时间。
    边界：
        - 只做初始化与日志，不调用 LLM、不写业务库；
        - 任何失败都不阻断启动：检索层已有 BM25 降级，预审仍可运行。
"""
import logging
import time

from app.config import settings
from app.rag.loader import get_policy_document
from app.rag.retriever import get_retriever


logger = logging.getLogger(__name__)



# 执行一次检索自检
def check_policy_backend() -> dict:
    """
    初始化检索后端并返回自检结果
        :return: {"enabled","configured","backend","clauses","degraded","error","elapsed_ms"}
    """
    started = time.perf_counter()                     # 开始时间
    result = {
        "enabled": bool(settings.POLICY_RAG_ENABLED),      # 制度 RAG 是否开启
        "configured": str(settings.POLICY_RETRIEVER),      # 配置的后端
        "backend": None,                                   # 实际使用的后端类名
        "clauses": 0,                                      # 条款数量
        "degraded": False,                                 # 是否发生降级
        "error": None,                                     # 初始化错误
        "elapsed_ms": 0,                                   # 初始化耗时
    }

    # 1.总开关关闭：不加载模型，直接返回
    if not result["enabled"]:
        logger.info("制度检索自检跳过：POLICY_RAG_ENABLED=false")
        return result

    # 2.初始化检索器：异常不抛出，只记录（预审会走空制度/BM25 降级）
    try:
        retriever = get_retriever()
    except Exception as exc:                          # noqa: BLE001 - 自检必须吞掉所有异常
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["degraded"] = True
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        logger.error(
            "制度检索自检失败，AI 预审将按无制度模式运行：%s",
            result["error"],
            exc_info=True,
        )
        return result

    # 3.记录实际后端与条款数量
    result["backend"] = type(retriever).__name__
    document = getattr(retriever, "document", None) or get_policy_document()
    result["clauses"] = len(getattr(document, "clauses", ()))

    # 4.配置了向量检索却回退到 BM25：明确告警
    configured = result["configured"].lower()
    if configured in ("chroma", "hybrid") and result["backend"] == "BM25PolicyRetriever":
        result["degraded"] = True
        logger.warning(
            "制度检索已降级为 BM25：配置=%s，实际=%s（检查 chromadb 与本地 ONNX 模型目录）",
            configured, result["backend"],
        )

    # 5.正常日志：启动时能看到后端、条款数与耗时
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info(
        "制度检索自检完成 backend=%s clauses=%s elapsed=%sms degraded=%s",
        result["backend"], result["clauses"], result["elapsed_ms"], result["degraded"],
    )
    return result
