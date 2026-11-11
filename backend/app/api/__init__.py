"""
HTTP 接口层（app/api）
    负责把 services 的业务能力暴露成 REST 接口，按版本渐进式扩展：
        - deps.py      全局鉴权依赖：从 Authorization: Bearer 解析并校验 JWT，返回当前登录用户
        - v1/          v1 版本子路由：auth / user / department / expense / approval
    所有接口统一挂载在 /api/v1 前缀下，由 `app/api/v1/router.py` 汇总成一个 api_router，
    再交给 `app/main.py` 中的 `app.include_router(api_router)` 注册到应用上。
"""
