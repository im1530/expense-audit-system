"""
AI 预审工作流包（Phase D）
    模块：
        - state.py：LangGraph 状态定义；
        - nodes.py：load_context/document/rule/rag/risk/decision 节点；
        - decision.py：确定性决策策略；
        - engine.py：LangGraph 图、节点包装、run/step 审计；
        - checkpoint.py：MySQL Checkpointer（Phase F，检查点持久化与恢复）；
        - service.py：对外入口 run_review_workflow / resume_review_workflow。
    边界：
        - 工作流只做编排，具体业务仍复用 Phase B/C 的 service/agent；
        - decision 只输出建议，不自动通过/驳回；
        - 运行审计写 workflow_runs/workflow_steps，评测数据另放文件。
"""
