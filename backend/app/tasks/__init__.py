"""
Celery 任务包（app/tasks）
    当前任务：
        - review_tasks.py：AI 预审执行任务
    约定：
        - 任务函数只做"取参数 → 调 service → 失败重试/兜底"，不写业务规则；
        - 任务必须幂等，重复投递时由 service 的原子领取保证只执行一次；
        - 单元测试不得依赖真实 Redis，队列开关由 AI_REVIEW_QUEUE_ENABLED 控制。
"""
