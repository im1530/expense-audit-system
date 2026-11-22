"""
规则数据模型（rules 表 + 规则命中记录表）
    存放规则引擎的校验规则，是"审批流匹配 / 合规校验"的配置来源：
        - rule_type：规则分类
            · amount                    —— 路由规则：按「角色 + 金额区间」决定走哪条审批流
            · check_category_limit      —— 检查规则：单条明细超过所属类别单次限额
            · check_invoice_missing     —— 检查规则：明细无发票且未填写无票说明
            · check_duplicate           —— 检查规则：同人/同日期/同类别/同金额在窗口期内重复
            · check_period_late         —— 检查规则：费用发生日距提交日超过 N 天
        - condition：触发条件的可读描述，用于后台展示与人工审核
        - config：机器可读的参数，JSON 结构随 rule_type 变化
        - action：引擎动作：amount 规则为 assign_chain，check_* 规则为 check
        - severity：检查规则风险级别（warn / block / review）；amount 规则为空
        - is_active：支持停用而不删除，便于灰度与回滚
    ExpenseRuleHit 存放每次提交时算出的命中结果（规则名/级别/提示/证据），供审批页展示与统计。
    说明：本模块只负责"配置与结果存储"，解析与执行逻辑在 services/rule_engine.py 与 rule_service.py。
"""



from sqlalchemy import Column, BigInteger, String, Text, Boolean, JSON, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import Base



# 报销校验规则表
class Rule(Base):
    """
    报销校验规则表模型：一条记录对应一条可配置的规则
        审批流匹配的典型配置（由 services.expense_service.match_approval_flow 解释执行）：
            rule_type = "amount"
            config    = {"submitter_roles": ["employee"], "min_amount": 0, "max_amount": 5000, "flow_id": 2}
        含义：员工提交、总金额在 0~5000 元之间的报销单，命中后绑定 ID=2 的审批流。
        字段约定：
            - name / rule_type / condition / action 必填，保证规则可读、可归类、可审计；
            - config 为 JSON，缺省键需在读取处兜底（如 `rule.config or {}`），不要假设键一定存在；
            - is_active 默认 True，停用的规则不参与匹配。
    """
    # 表名(显式写出)
    __tablename__ = "rules"                 # 表名：校验规则


    # ===== 主键 =====
    id = Column(
        BigInteger,             # 字段类型：大整数，适合做主键自增
        primary_key=True,       # 主键
        index=True,             # 建立索引,按 id 查询更快
        autoincrement=True,     # 自增,不用手动传id
        comment="校验规则ID"      # 字段备注，写进数据库
    )


    # ===== 规则基本信息 =====
    # name:规则名称,如"单笔金额超限"
    name = Column(
        String(100),            # 字段类型：字符串，长度为100
        nullable=False,         # 非空：每条规则必须有名称
        comment="规则名称"       # 字段备注，写进数据库
    )

    # rule_type:规则类型,如 amount_limit / invoice_check / duplicate
    rule_type = Column(
        String(50),             # 字段类型：字符串，长度为50
        index=True,             # 建立索引,按 rule_type 查询更快
        nullable=False,         # 非空：每条规则必须有类型
        comment="规则类型"       # 字段备注，写进数据库
    )

    # condition:触发条件(判断表达式或文字描述)
    condition = Column(
        Text,               # 字段类型：Text，不限长度(区别于 String 要设长度上线)
        nullable=False,     # 非空:每条规则必须有触发条件
        comment="触发条件"   # 字段备注，写进数据库
    )

    # action:引擎动作；amount 规则固定 assign_chain，check_* 规则固定 check
    action = Column(
        String(50),             # 字段类型：字符串，长度为50
        nullable=False,         # 非空：每条规则必须有引擎动作
        comment="引擎动作"       # 字段备注，写进数据库
    )

    # severity:检查规则的严重级别（warn/review/block）；amount 规则为空
    severity = Column(
        String(20),             # 字段类型：字符串，长度为20
        nullable=True,          # amount 规则没有严重级别
        comment="风险级别"       # 字段备注，写进数据库
    )


    # ===== 规则配置 =====
    # config:规则参数，用 JSON 存，如 {"max_amount": 5000}
    config = Column(
        JSON,                 # JSON 类型：存阈值、参数等灵活结构
        comment="规则配置"     # 字段备注，写进数据库
    )


    # ===== 状态 =====
    # is_active：是否启用，停用的规则不参与校验
    is_active = Column(
        Boolean,                # 字段类型：布尔值
        default=True,           # 默认值：True，表示默认启用
        comment="是否启用"       # 字段备注，写进数据库
    )


    # ===== 时间戳 =====
    # created_at 记录创建时间 / updated_at 记录最后修改时间 已经在 base.py里写好


    # ===== 调试打印显示直观信息 =====
    def __repr__(self):
        """
        调试打印用：决定对象被 print()、在调试器查看或放进列表打印时显示的字符串
            :return: 形如 <Rule(id=1, name=单笔金额超限, type=amount)>，便于快速定位是哪条规则
        """
        return f"<Rule(id={self.id}, name={self.name}, type={self.rule_type})>"



# 规则命中记录表
class ExpenseRuleHit(Base):
    """
    规则命中记录：一次提交（或重新提交）里，某条规则命中某个对象的证据
        用途：
            - 审批页展示"为什么会提示/拦截"，让审批人有依据地判断；
            - 统计规则命中率，反哺阈值调优；
            - 未来 LLM 审核时作为上下文与对照基线。
        字段说明：
            - rule_name / rule_type 是快照：规则以后被删除或改名，历史命中仍可读；
            - rule_id 不建外键：避免规则删除时历史记录被连带清理；
            - evidence 存实际值、阈值、关联单据等结构化证据，前端口径统一。
    """
    # 表名(显式写出)
    __tablename__ = "expense_rule_hits"     # 表名：规则命中记录

    # ===== 主键 =====
    id = Column(
        BigInteger,                         # 大整数
        primary_key=True,                   # 主键
        index=True,                         # 建索引
        autoincrement=True,                 # 自增
        comment="命中记录ID"                 # 字段备注
    )

    # ===== 外键:关联报销单 =====
    expense_id = Column(
        BigInteger,                                     # 与 expenses.id 同类型
        ForeignKey("expenses.id", ondelete="CASCADE"),  # 单据删除时级联清理命中记录
        nullable=False,                                 # 非空
        index=True,                                     # 按单据查询
        comment="报销单ID"                               # 字段备注
    )

    # ===== 规则快照 =====
    rule_id = Column(
        BigInteger,                         # 规则ID(可空:规则可能已被删除)
        comment="规则ID"                     # 字段备注
    )

    rule_type = Column(
        String(50),                         # 规则类型快照
        nullable=False,                     # 非空
        comment="规则类型快照"                # 字段备注
    )

    rule_name = Column(
        String(100),                        # 规则名称快照
        nullable=False,                     # 非空
        comment="规则名称快照"                # 字段备注
    )

    # ===== 命中结果 =====
    level = Column(
        String(20),                         # 级别：warn / block / review
        nullable=False,                     # 非空
        comment="级别(warn/block/review)"    # 字段备注
    )

    message = Column(
        String(500),                        # 给审批人看的提示
        nullable=False,                     # 非空
        comment="提示文案"                    # 字段备注
    )

    evidence = Column(
        JSON,                               # 命中证据(JSON)
        comment="命中证据"                    # 字段备注
    )

    # 命中记录是"提交时算出的快照"，只写不改，因此不需要 updated_at。
    # 显式置 None 可取消从 Base 继承来的 updated_at 列（SQLAlchemy 支持用 None 移除 mixin 字段）。
    updated_at = None                       # 命中记录只写不改，移除基类继承的 updated_at

    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 这条命中属于哪张报销单
    expense = relationship(
        "Expense",                          # 关联目标模型
        back_populates="rule_hits",         # 配对 Expense.rule_hits
    )

    # ===== 调试打印显示直观信息 =====
    def __repr__(self) -> str:
        """
        调试打印用：决定对象被 print()、调试器查看时的显示内容
            :return: 形如 <ExpenseRuleHit(id=1,rule=类别限额,level=warn)>
        """
        return f"<ExpenseRuleHit(id={self.id},rule={self.rule_name},level={self.level})>"




