"""
AI Agent 包（Phase B/C/D：规则 + 制度检索 + 凭证识别 + 风险评审 + 工作流）
    目录：
        - context.py：把报销单转成模型可读、已脱敏、已截断的上下文；
        - prompts.py：System/User Prompt，版本化，注入 <policy_context>；
        - document_agent.py：发票/行程单/支付截图的多模态识别与字段归一化；
        - risk_agent.py：调用 LLM、校验输出、规则/凭证优先级归一化、引用校验。
    制度检索与引用校验在 app/rag/ 与 app/services/policy_service.py；
    凭证持久化与比对在 app/services/document_service.py / document_rules.py；
    Phase D 工作流在 app/workflow/（LangGraph 编排 document → rule → rag → risk → decision）。
"""
