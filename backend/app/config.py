"""
应用配置管理（全局唯一配置源）
    使用 Pydantic Settings 从 .env 文件加载并校验配置，启动时一次性完成类型转换与合法性检查，配置错误会直接启动失败。
    **Pydantic 是 Python 的数据校验库**：
    用类型注解声明数据长什么样，它自动检查 "类型对不对、缺不缺字段、能不能转换"，不对就直接报错，不用自己写一堆 if 判断。
    使用方式：
        - 全项目通过 `from app.config import settings` 读取配置，不要直接读环境变量；
        - 新增配置项只需在 Settings 中加一个带类型注解的字段，并在 .env 中给值，默认值可兜底；
        - 字段名大小写敏感（case_sensitive=True），.env 里的键必须与字段名完全一致。
    当前分组：应用基础、可信主机、CORS 白名单、日志级别、数据库连接、JWT 令牌、附件、AI 预审、制度 RAG、凭证识别。
"""
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    应用配置类：每个大写字段就是一个配置项，值优先取自 .env 文件，缺省时回落到代码里的默认值
        说明：
            - 字段名即 .env 中的键名，大小写敏感，不要随意改名，否则会读不到值；
            - 带默认值的字段可不在 .env 中出现；无合理默认值的字段（如 DATABASE_URL、SECRET_KEY）上线前必须显式配置；
            - 列表类型字段（ALLOWED_HOSTS / ALLOWED_ORIGINS）在 .env 中用 JSON 数组写法，如 ["localhost"]；
            - 本类在模块末尾实例化为 settings，全项目共用这一份，不要再自行创建实例。
    """
    # ===== 应用基础 =====
    APP_NAME: str = "AI Agent 财务报销审核系统"   # 应用名称
    APP_ENV: str = "development"                # 当前环境：development 开发 / production 上线
    DEBUG: bool = True                          # 调试模式，上线时改成 False
    SECRET_KEY: str = ""                        # 应用主密钥（占位，上线换随机长串）


    # ===== 可信主机（main.py 的 TrustedHost 中间件用）=====
    ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1"]    # 可信主机白名单


    # ===== CORS 跨域白名单（main.py 的 CORS 中间件用）=====
    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:3000",  # Vue 开发服务器常用端口
        "http://localhost:5173",
    ]


    # ===== 日志配置（core/logging.py 读取）=====
    LOG_LEVEL: str = "INFO"                      # 日志级别：DEBUG/INFO/WARNING/ERROR
    LOG_FILE: str = ""                           # 日志文件路径；留空只输出控制台
    LOG_RETENTION_DAYS: int = 14                 # 日志文件保留天数（按天轮转）
    SLOW_REQUEST_MS: int = 500                   # 慢请求阈值（毫秒），超过打 WARNING

    # ===== 接口限流（进程内滑动窗口；多 worker 各自独立计数）=====
    RATE_LIMIT_ENABLED: bool = True              # 总开关：关闭后所有接口不限流
    RATE_LIMIT_BACKEND: str = "auto"             # 存储后端：auto（优先 Redis，失败回退）/ redis / memory
    RATE_LIMIT_WINDOW_SECONDS: int = 60          # 统计窗口（秒）
    RATE_LIMIT_DEFAULT_PER_WINDOW: int = 120     # 默认每窗口请求数（0 表示不限）
    RATE_LIMIT_LOGIN_PER_WINDOW: int = 10        # 登录：每窗口最多尝试次数（防爆破）
    RATE_LIMIT_REGISTER_PER_WINDOW: int = 5      # 注册：每窗口最多建号次数
    RATE_LIMIT_AI_TRIGGER_PER_WINDOW: int = 5    # AI 预审触发：每窗口最多触发次数（会调 LLM）
    RATE_LIMIT_UPLOAD_PER_WINDOW: int = 30       # 附件上传：每窗口最多上传次数
    RATE_LIMIT_EXEMPT_PATHS: list[str] = [       # 不限流的路径（探活与文档）
        "/",
        "/health",
        "/api/docs",
        "/api/redoc",
        "/openapi.json",
    ]


    # 配置从 .env 文件读取；case_sensitive 表示字段名大小写敏感
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)    # 从 .env 读取，键名大小写敏感


    # ===== 数据库配置：真实值从 .env 读取 =====
    DATABASE_URL: str = ""                       # MySQL 连接串（从 .env 读取）


    # ===== JWT 配置 =====
    JWT_ALGORITHM: str = "HS256"                 # 签名算法，固定 HS256
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24   # token 有效期 24 小时（单位分钟）


    # ===== 附件上传（发票 / 凭证）=====
    UPLOAD_DIR: str = "uploads"                  # 存储根目录：相对 backend 目录，也可填绝对路径
    MAX_UPLOAD_MB: float = 9.9                   # 单个文件大小上限（MB），9.9MB
    ALLOWED_UPLOAD_MIME_TYPES: list[str] = [     # 允许的文件类型白名单（按文件头嗅探结果判断）
        "image/jpeg",
        "image/png",
        "application/pdf",
    ]
    ATTACHMENT_URL_EXPIRE_MINUTES: int = 30      # 签名下载链接有效期（分钟）
    PUBLIC_BASE_URL: str = ""                    # 对外访问前缀；留空则返回相对路径，由前端拼当前域名
    ATTACHMENT_ENCRYPTION_KEY: SecretStr = SecretStr("")   # 附件落盘加密密钥（Fernet）；留空表示不加密


    # ===== LLM（DeepSeek，OpenAI 兼容协议）=====
    DEEPSEEK_API_KEY: SecretStr = SecretStr("")           # API Key：用 SecretStr 防止日志/异常打印明文
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"   # 接口地址（OpenAI 兼容）
    DEEPSEEK_MODEL: str = "deepseek-flash"                # 模型名
    DEEPSEEK_DISABLE_SYSTEM_PROXY: bool = True            # 是否绕过系统代理（Windows 代理失效时会 10061）

    # ===== AI 预审（风险评审 Agent）=====
    AI_REVIEW_ENABLED: bool = True                        # 总开关：关闭后不调用 LLM，直接走规则兜底
    AI_REVIEW_TIMEOUT_SECONDS: float = 30.0               # 单次 HTTP 调用超时（秒）
    AI_REVIEW_MAX_RETRIES: int = 1                        # 失败重试次数（不含首次）
    AI_REVIEW_TEMPERATURE: float = 0.1                    # 采样温度：审核场景要稳定，不能发散
    AI_REVIEW_MAX_TOKENS: int = 1200                      # 输出 token 上限，控制成本
    AI_REVIEW_DISABLE_THINKING: bool = True               # 结构化预审关闭思考模式，避免 JSON 被 reasoning 截断
    AI_REVIEW_PROMPT_VERSION: str = "risk_review_v4_2_citation_guard"   # Prompt 版本号，写入审计表便于复盘
    AI_REVIEW_MAX_INPUT_ITEMS: int = 50                   # 单次最多送入多少条明细，防止上下文超长
    AI_REVIEW_CACHE_MINUTES: int = 10                     # 成功结果缓存时长（分钟），避免重复调用
    AI_REVIEW_DAILY_LIMIT: int = 200                      # 全站每日调用上限（成本保护）
    AI_REVIEW_STALE_MINUTES: int = 10                     # pending/running 超过该时长视为僵尸任务
    AI_REVIEW_AUTO_TRIGGER: bool = True                   # 提交/重提后是否自动触发预审
    AI_REVIEW_QUEUE_ENABLED: bool = True                  # 是否投递 Celery 队列；关闭时回退 BackgroundTasks
    AI_REVIEW_TASK_MAX_RETRIES: int = 2                   # Celery 任务失败后的最大重试次数
    AI_REVIEW_RAW_RESPONSE_ENABLED: bool = False          # 是否保存模型原始返回（默认关闭，数据最小化）
    DOCUMENT_RAW_RESPONSE_ENABLED: bool = False           # 是否保存凭证识别原始返回（默认关闭）
    DOCUMENT_AI_CONSENT_REQUIRED: bool = True             # 是否要求用户显式授权后才把影像发给模型
    DOCUMENT_AI_CONSENT_VERSION: str = "v1"               # 当前授权条款版本，版本变化后需重新授权
    DOCUMENT_DAILY_LIMIT: int = 200                       # 凭证识别每日调用上限（0 表示不限）
    MODEL_INPUT_PRICE_PER_1M: float = 0.0                 # 模型输入价格（元/百万 token，0 表示未配置）
    MODEL_OUTPUT_PRICE_PER_1M: float = 0.0                # 模型输出价格（元/百万 token，0 表示未配置）
    AI_REVIEW_DAILY_TOKEN_LIMIT: int = 0                  # 每日 token 预算（0 表示不限）
    AI_REVIEW_DAILY_COST_LIMIT: float = 0.0               # 每日费用预算（元，0 表示不限）

    # ===== 制度 RAG（Phase B/D：结构化条款 + BM25 / Chroma 向量检索）=====
    POLICY_RAG_ENABLED: bool = True                      # 总开关：关闭后不检索制度，引用必须为空
    POLICY_RETRIEVER: str = "chroma"                     # 检索后端：chroma（默认）/ bm25 / hybrid
    POLICY_DIR: str = "../docs/policies"                 # 制度目录：相对 backend 目录，也可填绝对路径
    POLICY_FILE: str = "expense_policy_v1.md"            # 当前使用的制度文件名
    POLICY_TOP_K: int = 4                                # 单次最多注入多少条制度条款
    POLICY_MIN_SCORE: float = 0.6                        # BM25 绝对分数下限，低于该值不返回
    POLICY_MIN_RELATIVE_SCORE: float = 0.35              # 相对最高分的比例下限，过滤明显不相关条款
    POLICY_CHROMA_DIR: str = "chroma_policy"             # 向量库持久化目录（相对 backend 或绝对路径）
    POLICY_CHROMA_COLLECTION: str = "policy_clauses"     # 向量集合名
    POLICY_CHROMA_MIN_SIMILARITY: float = 0.20           # 余弦相似度下限（低于不返回）
    POLICY_EMBEDDING_DIR: str = ""                       # 本地 embedding 模型目录；空=用户缓存默认目录
    POLICY_EMBEDDING_POOLING: str = "cls"                # 池化方式：mean（MiniLM）/ cls（BGE）
    POLICY_EMBEDDING_MAX_LENGTH: int = 256               # 向量化时的最大 token 长度
    POLICY_STARTUP_CHECK: bool = True                    # 启动时初始化检索后端并打日志（模型/向量库自检）
    POLICY_MAX_CONTEXT_CHARS: int = 3500                 # 注入 Prompt 的制度上下文最大字符数
    POLICY_CITATION_MAX: int = 5                         # 单次预审最多保留多少条有效引用
    POLICY_CITATION_MIN_QUOTE_CHARS: int = 8             # 引用原文的最小长度（去空白后）

    # ===== Phase C：凭证/发票识别（document_agent）=====
    DOCUMENT_AI_ENABLED: bool = True                      # 总开关：关闭后不做凭证识别，也不产生凭证类风险
    DOCUMENT_PROMPT_VERSION: str = "document_extract_v1" # 凭证识别 Prompt 版本（写入审计）
    DOCUMENT_MAX_TOKENS: int = 800                        # 单次识别输出 token 上限
    DOCUMENT_TIMEOUT_SECONDS: float = 45.0                # 单次多模态调用超时（秒）
    DOCUMENT_MAX_RETRIES: int = 1                         # 失败重试次数（不含首次）
    DOCUMENT_MAX_ATTACHMENTS: int = 5                     # 单张报销单最多识别多少张凭证
    DOCUMENT_IMAGE_MAX_EDGE: int = 1600                   # 图片最长边（像素），不放大
    DOCUMENT_IMAGE_MAX_MB: float = 2.0                    # 单张图片压缩后的目标大小（MB）
    DOCUMENT_PDF_MAX_PAGES: int = 2                       # 单份 PDF 最多渲染多少页
    DOCUMENT_PDF_RENDER_SCALE: float = 2.0                # PDF 渲染倍率（2.0 约 144 DPI）
    DOCUMENT_DATE_TOLERANCE_DAYS: int = 7                 # 票面日期与费用日期允许的最大偏差（天）
    DOCUMENT_AMOUNT_TOLERANCE: float = 0.01               # 金额比对容差（元）
    DOCUMENT_MIN_CONFIDENCE: float = 0.5                  # 低于该置信度标记人工复核
    DOCUMENT_WEAK_EVIDENCE_MAX_AMOUNT: float = 200.0      # 弱凭证免复核金额上限（可按类别覆盖）
    DOCUMENT_MEDIUM_EVIDENCE_MAX_AMOUNT: float = 1000.0   # 中等凭证免复核金额上限
    COMPANY_IDENTITIES: list[dict[str, str]] = [         # 允许的购买方抬头（支持集团/多主体）
        {"name": "示例科技有限公司", "tax_id": "91440101MA5TEST001"},   # 测试主体一
        {"name": "示例服务有限公司", "tax_id": "91440101MA5TEST002"},   # 测试主体二
    ]

    # ===== Phase D：工作流编排 =====
    WORKFLOW_ENABLED: bool = True                        # 是否启用显式工作流（关闭则走旧线性流程）
    WORKFLOW_ENGINE: str = "langgraph"                   # 编排引擎：langgraph / simple（预留）
    WORKFLOW_CHECKPOINT_ENABLED: bool = True             # 是否使用 MySQL 持久化检查点（关闭回退 MemorySaver）
    WORKFLOW_NODE_MAX_ATTEMPTS: int = 2                  # 单节点最大尝试次数（含首次）
    WORKFLOW_NODE_RETRY_BACKOFF_SECONDS: float = 1.0     # 节点重试初始退避（秒）
    WORKFLOW_NODE_RETRY_MAX_BACKOFF_SECONDS: float = 10.0   # 节点重试最大退避（秒）
    WORKFLOW_STALE_MINUTES: int = 15                     # running 超过该时长视为僵尸 run，由恢复脚本收尾


    # ===== Phase F：异步任务队列（Celery + Redis）=====
    REDIS_URL: str = "redis://127.0.0.1:6379/0"         # 本地 Redis 默认地址（Memurai 默认 6379）
    CELERY_BROKER_URL: str = ""                          # 消息中间站地址；留空回落到 REDIS_URL
    CELERY_RESULT_BACKEND: str = ""                      # 任务结果存储；留空回落到 REDIS_URL
    CELERY_TASK_ALWAYS_EAGER: bool = False               # 本地同步执行任务（仅测试/排障）
    CELERY_TASK_TIME_LIMIT_SECONDS: int = 300            # 单任务硬超时（秒），防止 worker 卡死


    # ===== 隐私与留存（Phase F）=====
    DATA_RETENTION_DAYS: int = 90                        # 发票/凭证文件与检查点保留天数
    RAW_RESPONSE_RETENTION_DAYS: int = 30                # 历史 raw_response 最长保留天数（到期置空）




# 全局唯一配置实例，别处直接 from app.config import settings 使用
settings = Settings()      # 全局唯一配置实例，别处直接 from app.config import settings