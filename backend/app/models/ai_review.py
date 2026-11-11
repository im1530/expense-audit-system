"""
AI 预审审计模型（ai_review_logs 表 + 评审状态枚举）
    为什么单独建表而不是只写 expenses.risk_*：
        - 一张单可能被多次预审（手动重跑、模型升级、规则变更后复算），只存最新值会丢历史；
        - 出问题时要能回答"当时用的哪个模型、哪版 Prompt、耗时多少、是否降级、原始返回是什么"；
        - AI 结论是概率性结果，必须可追溯、可对比、可回滚，不能覆盖式写入。
    状态机（由 service 层保证原子流转）：
        pending → running → success / fallback / failed
        - success：LLM 正常返回并通过结构校验；
        - fallback：LLM 不可用/超时/输出非法，改用规则引擎结论兜底；
        - failed：连兜底都无法完成（如数据库写入失败）。
    约定：
        - 本表只追加与状态更新，不物理删除（删报销单时由外键级联清理）；
        - raw_response 可能含单据文本，仅管理员/审计可见，接口不直接透出；
        - risk_level 取 low / mid / high，与规则风险的 warn/review/block 是两套体系。
"""
import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.models.base import Base



# AI 预审任务状态
class AIReviewStatus(str, enum.Enum):
    """
    AI 预审任务状态枚举（描述"这一次预审"走到哪一步）
        - PENDING：任务已创建，等待后台执行；
        - RUNNING：后台已领取，正在调用 LLM；
        - SUCCESS：LLM 正常返回且通过结构校验；
        - FALLBACK：LLM 失败/超时/输出非法，已降级为规则兜底结论；
        - FAILED：连兜底都失败（如数据库异常），需要人工排查。
    """
    PENDING = "pending"       # 待执行
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FALLBACK = "fallback"     # 已降级为规则兜底
    FAILED = "failed"         # 失败



# AI 预审审计日志表
class AIReviewLog(Base):
    """
    AI 预审审计日志：一次预审一行，记录输入、输出、模型与降级信息
        字段分组：
            - 归属：expense_id（被审单据）、triggered_by（触发人）、trace_id（链路追踪）；
            - 执行：status、trigger_source、provider、model、prompt_version、policy_version；
            - 结果：risk_score、risk_level、summary、findings、policy_citations；
            - 诊断：raw_response、retrieval_meta、error_message、is_fallback、latency_ms、token 用量。
        注意：
            - risk_score 允许为空（pending/running/failed 时没有分数）；
            - findings / policy_citations / retrieval_meta 是 JSON，结构由 schema 层校验；
            - 本表不存 API Key，也不存用户手机号/邮箱等无关隐私字段。
    """
    __tablename__ = "ai_review_logs"

    # ===== 主键 =====
    id = Column(
        BigInteger,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="预审记录ID",
    )

    # ===== 归属 =====
    expense_id = Column(
        BigInteger,
        ForeignKey("expenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="被审报销单ID（删单时级联清理）",
    )
    triggered_by = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="触发人ID（用户删除后置空，保留审计记录）",
    )
    trace_id = Column(
        String(64),
        nullable=True,
        index=True,
        comment="链路追踪ID（与日志 request_id 对应）",
    )

    # ===== 执行信息 =====
    status = Column(
        Enum(
            AIReviewStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            name="ai_review_status",
        ),
        nullable=False,
        default=AIReviewStatus.PENDING,
        index=True,
        comment="任务状态：pending/running/success/fallback/failed",
    )
    trigger_source = Column(
        String(20),
        nullable=False,
        default="manual",
        comment="触发来源：manual 手动 / submit 提交自动",
    )
    provider = Column(
        String(50),
        nullable=False,
        default="deepseek",
        comment="LLM 提供方",
    )
    model = Column(
        String(100),
        nullable=False,
        default="",
        comment="模型名（如 deepseek-flash）",
    )
    prompt_version = Column(
        String(50),
        nullable=False,
        default="",
        comment="Prompt 版本号（复盘时定位提示词）",
    )
    policy_version = Column(
        String(50),
        nullable=False,
        default="",
        comment="制度版本号（本次引用的是哪一版制度）",
    )

    # ===== 评审结果 =====
    risk_score = Column(
        Numeric(5, 2),
        nullable=True,
        comment="AI 风险分数 0~100",
    )
    risk_level = Column(
        String(20),
        nullable=True,
        comment="AI 风险等级：low/mid/high",
    )
    summary = Column(
        Text,
        nullable=True,
        comment="AI 总结",
    )
    findings = Column(
        JSON,
        nullable=True,
        comment="风险点列表（类型/级别/说明/证据/建议）",
    )
    policy_citations = Column(
        JSON,
        nullable=True,
        comment="通过校验的制度引用",
    )
    retrieval_meta = Column(
        JSON,
        nullable=True,
        comment="制度检索审计信息（检索条款、丢弃原因等）",
    )
    decision = Column(
        String(30),
        nullable=True,
        comment="建议决策：fast_track/manual_review/suggest_reject",
    )
    decision_reason = Column(
        String(500),
        nullable=True,
        comment="建议决策说明",
    )
    workflow_run_id = Column(
        BigInteger,
        nullable=True,
        index=True,
        comment="对应工作流运行ID（Phase D）",
    )

    # ===== 诊断与成本 =====
    raw_response = Column(
        Text,
        nullable=True,
        comment="模型原始返回（排障用，不通过接口透出）",
    )
    error_message = Column(
        String(500),
        nullable=True,
        comment="失败原因（截断保存，避免超长）",
    )
    is_fallback = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否规则兜底结论",
    )
    latency_ms = Column(
        Integer,
        nullable=True,
        comment="本次调用耗时（毫秒）",
    )
    prompt_tokens = Column(
        Integer,
        nullable=True,
        comment="输入 token 数",
    )
    completion_tokens = Column(
        Integer,
        nullable=True,
        comment="输出 token 数",
    )
    cost = Column(
        Numeric(12, 6),
        nullable=True,
        comment="本次预审估算费用（元）",
    )

    # ===== 关联关系（ORM 导航，不产生新字段）=====
    expense = relationship("Expense")     # 所属报销单

    def __repr__(self) -> str:
        """
        调试打印：只暴露定位信息，不打印模型原始返回
            :return: 形如 <AIReviewLog(id=1, expense_id=2, status=success)>
        """
        return f"<AIReviewLog(id={self.id}, expense_id={self.expense_id}, status={self.status})>"
