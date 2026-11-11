"""
Phase D/F 评测包
    - cases/：冒烟集与 100 条金标准草稿（JSON，含真实评测 context）；
    - generate_golden.py：生成/更新金标准草稿；
    - harness.py：批量运行与指标计算；
    - runner.py：真实 LLM 运行、结果录制与回放；
    - compare.py：Prompt/模型版本报告对比；
    - reports/：生成的评测报告（JSON/Markdown）。
    说明：
        - 案例标签是 draft，需业务/财务复核；
        - mock 模式使用 simulated 结果验证决策层，不代表真实模型准确率；
        - real 模式会真实调用模型并产生费用，执行前需确认授权。
"""
