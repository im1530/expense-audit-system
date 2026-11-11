"""
业务服务层（app/services）
    沉淀跨接口复用、与 HTTP 无关的业务逻辑，供路由层调用，便于复用与单元测试：
        - expense_service.py：报销单/明细通用筛选条件拼装、审批流匹配、报销单总金额重算
        - policy_service.py：制度检索（Phase B），失败自动降级为无制度模式
        - document_preprocess.py：凭证图片/PDF 预处理（缩放、EXIF、PDF 转图）
        - document_rules.py：凭证与明细的一致性规则、证据强度阈值
        - document_service.py：凭证识别任务、缓存、落库与风险汇总
        - ai_review_service.py：AI 预审任务创建、执行、幂等/缓存/降级/审计
        - review_queue.py：预审任务投递（Celery 优先，BackgroundTasks 回退）与提交后自动触发
        - privacy_service.py：AI 数据处理授权判断与统一提示文案
        - cost_service.py：token/费用估算、每日预算与凭证识别限额
    分层约定：本层只接收 db 会话与领域对象，不构造 HTTP 响应、不做身份认证，
    出错时直接抛出 HTTPException 由 FastAPI 统一转成标准错误响应。
"""
