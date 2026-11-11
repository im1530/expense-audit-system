"""
核心基础设施包（app/core）
    沉淀与业务无关、被全局复用的底层能力，目前包含：
        - security.py：明文密码的 bcrypt 单向哈希与校验、JWT 访问令牌的签发与验签
    分层约定：本包只允许依赖 config，不允许反向依赖 api / services / models，避免循环导入。
"""
