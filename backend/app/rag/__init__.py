"""
制度 RAG 包（Phase B/D：结构化条款 + BM25 / Chroma 向量检索）
    模块划分：
        - schema.py：条款/文档/查询/命中/结果集数据结构；
        - tokenizer.py：归一化、中文 bigram、同义词、引用比对；
        - filters.py：applies_to（费用大类/角色/规则/金额）过滤，BM25 与向量共用；
        - loader.py：Markdown 解析、校验、进程级缓存；
        - retriever.py：PolicyRetriever 协议 + BM25 实现 + 按配置创建（工厂 + 降级）；
        - embedding.py：本地 ONNX 模型向量化（默认 bge-small-zh-v1.5，CLS 池化）；
        - chroma_retriever.py：Chroma 持久化集合 + 余弦检索；
        - hybrid_retriever.py：BM25 优先 + 向量补位的混合检索；
        - citation.py：引用白名单、原文子串校验、审计统计。
    边界：
        - 本包只做"制度读取、检索、引用校验"，不调用 LLM、不写业务数据库；
        - 向量后端不可用（缺依赖/缺模型）时工厂自动降级 BM25，不阻断预审。
"""
