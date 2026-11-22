"""
规则模块数据模型（管理端规则维护 + 规则命中结果）
    规则表 rules 是"审批流匹配 / 合规校验"的配置来源，管理端围绕它做增删改查。
    - AdminRuleItem / AdminRuleCreate / AdminRuleUpdate / AdminRuleListResponse：管理端 CRUD；
    - RuleHitItem / RulePrecheckResponse：规则引擎在提交/预检时产出的命中结果。

    rule_type 与 config 约定：
        amount                 路由规则，config = {"submitter_roles": [...], "min_amount": 0,
                                                  "max_amount": 999.99, "flow_id": 2}
        check_category_limit   类别限额，config = {}
        check_invoice_missing  发票缺失，config = {}
        check_duplicate        重复报销，config = {"window_days": 60}
        check_period_late      跨期报销，config = {"max_days": 60}
    具体合法性由 services/rule_service.py 校验。
"""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field



# ============ 规则命中结果（提交时生成，供审批页展示）============
class RuleHitItem(BaseModel):
    """
    规则命中项：一条规则命中一个具体对象（单据/明细）的提示
        :ivar id: 命中记录ID（预检时为 null，未落库）
        :ivar rule_id: 规则ID；规则被删除后仍保留名称快照
        :ivar rule_type: 规则类型，如 check_invoice_missing
        :ivar rule_name: 规则名称
        :ivar clause_id: 命中的制度条款ID，如 2.5
        :ivar level: 级别：warn（提示，允许提交）/ block（阻止提交）/ review（强制人工复核）
        :ivar message: 给审批人看的提示文案
        :ivar evidence: 命中证据（实际值、阈值、关联单据等）
    """
    id: Optional[int] = None                    # 命中记录ID(预检时为空)
    rule_id: Optional[int] = None               # 规则ID
    rule_type: str                              # 规则类型
    rule_name: str                              # 规则名称
    clause_id: Optional[str] = None             # 制度条款ID
    level: str                                  # warn/block/review
    message: str                                # 提示文案
    evidence: Optional[dict] = None             # 命中证据



# 规则预检响应
class RulePrecheckResponse(BaseModel):
    """
    规则预检响应（不改变单据状态，只告诉前端"如果现在提交会命中什么"）
        :ivar level: 汇总后的最高级别：ok（无命中）/ warn / block / review
        :ivar total: 命中条数
        :ivar hits: 命中明细
    """
    level: str = "ok"                           # 汇总级别
    total: int = 0                              # 命中条数
    hits: List[RuleHitItem] = []                # 命中明细



# ============ 规则风险标记（审批待办/已办列表用）============
class RuleRiskMark(BaseModel):
    """
    风险标记项：把一条规则命中翻译成审批人可直接阅读的风险提示
        与 RuleHitItem 的区别：这里额外给出中文短标签、级别中文名与关联明细，
        前端不用再根据 rule_type / message 自己拼接"这是什么风险"。
        :ivar rule_id: 规则ID；规则已删除时为原ID（历史快照仍可读）
        :ivar rule_type: 规则类型，如 check_category_limit
        :ivar rule_label: 列表短标签，如"明细超类别限额"（前端徽标直接显示）
        :ivar rule_hint: 这条规则在查什么（tooltip 解释用）
        :ivar rule_name: 规则名称快照
        :ivar clause_id: 命中的制度条款ID
        :ivar level: 级别：warn（提示）/ review（需复核）/ block（阻止提交）
        :ivar level_label: 级别中文名：提示 / 需复核 / 阻止提交
        :ivar message: 具体提示，已包含明细名称、金额、阈值等细节
        :ivar item_id: 风险落在哪条明细；单据级风险为 null
        :ivar item_description: 关联明细的费用说明；单据级风险为 null
        :ivar evidence: 命中证据（实际值、阈值、关联单据等）
    """
    rule_id: Optional[int] = None               # 规则ID
    rule_type: str                              # 规则类型
    rule_label: str                             # 列表短标签
    rule_hint: str = ""                         # 规则解释
    rule_name: str                              # 规则名称快照
    clause_id: Optional[str] = None             # 制度条款ID
    level: str                                  # warn/review/block
    level_label: str                            # 级别中文名
    message: str                                # 具体提示文案
    item_id: Optional[int] = None               # 关联明细ID
    item_description: Optional[str] = None      # 关联明细说明
    evidence: Optional[dict] = None             # 命中证据



# ============ 管理端：规则命中率统计 ============
class RuleHitStatItem(BaseModel):
    """
    单条规则的命中统计
        口径：hit_count 是命中记录条数（一条明细命中一次算一条），
        expense_count 是涉及的去重单据数，hit_rate = expense_count / 已提交单据数。
        :ivar rule_id: 规则ID
        :ivar rule_name: 规则名称
        :ivar rule_type: 规则类型
        :ivar rule_label: 规则类型中文短标签
        :ivar is_active: 规则当前是否启用
        :ivar hit_count: 命中记录条数
        :ivar expense_count: 涉及的报销单数（去重）
        :ivar warn_count: warn 级命中条数
        :ivar review_count: review 级命中条数
        :ivar block_count: block 级命中条数
        :ivar hit_rate: 命中率（涉及单据数 / 统计区间内已提交单据数）
    """
    rule_id: Optional[int] = None                   # 规则ID
    rule_name: str                                  # 规则名称
    rule_type: str                                  # 规则类型
    rule_label: str                                 # 类型中文短标签
    is_active: bool                                 # 是否启用
    hit_count: int = 0                              # 命中记录条数
    expense_count: int = 0                          # 涉及单据数
    warn_count: int = 0                             # warn 级条数
    review_count: int = 0                           # review 级条数
    block_count: int = 0                            # block 级条数
    hit_rate: float = 0.0                           # 命中率



class RuleHitStatsResponse(BaseModel):
    """
    规则命中率统计响应（管理端规则治理：看哪些规则真正在起作用、哪些阈值需要调）
        :ivar start_date: 统计起始日期（按提交时间，未传表示不限）
        :ivar end_date: 统计结束日期（含当天，未传表示不限）
        :ivar submitted_total: 统计区间内已提交的报销单数（命中率分母）
        :ivar hit_expense_count: 至少命中一条规则的报销单数
        :ivar total_hits: 命中记录总条数
        :ivar overall_hit_rate: 整体命中率（hit_expense_count / submitted_total）
        :ivar items: 逐条规则的命中统计，按命中条数倒序
    """
    start_date: Optional[date] = None               # 统计起始日期
    end_date: Optional[date] = None                 # 统计结束日期
    submitted_total: int = 0                        # 已提交单据数
    hit_expense_count: int = 0                      # 有命中的单据数
    total_hits: int = 0                             # 命中记录总数
    overall_hit_rate: float = 0.0                   # 整体命中率
    items: List[RuleHitStatItem] = []               # 逐规则统计



# 规则列表/详情出参
class AdminRuleItem(BaseModel):
    """
    管理端规则出参模型
        :ivar id: 规则ID
        :ivar name: 规则名称
        :ivar rule_type: 规则类型，目前审批流匹配只用 "amount"
        :ivar condition: 触发条件的可读描述（给管理员看的文字说明）
        :ivar action: 引擎动作：amount 规则为 assign_chain，check_* 规则为 check
        :ivar severity: 检查规则风险级别：warn/review/block；amount 规则为 null
        :ivar config: 规则参数（JSON），amount 类型含 submitter_roles / min_amount / max_amount / flow_id
        :ivar is_active: 是否启用（停用的规则不参与匹配）
        :ivar flow_id: 从 config 中解析出的目标审批流ID，非 amount 规则为 null
        :ivar flow_name: 目标审批流名称
        :ivar flow_is_active: 目标审批流是否启用（为 false 说明这条规则绑到了停用流程上，提交会报错）
        :ivar created_at: 创建时间
        :ivar updated_at: 最后更新时间
    """
    id: int                                     # 规则ID
    name: str                                   # 规则名称
    rule_type: str                              # 规则类型
    condition: str                              # 触发条件描述
    action: str                                 # 引擎动作
    severity: Optional[str] = None              # 检查规则风险级别
    config: Optional[dict] = None               # 规则参数
    is_active: bool                             # 是否启用
    flow_id: Optional[int] = None               # 目标审批流ID
    flow_name: Optional[str] = None             # 目标审批流名称
    flow_is_active: Optional[bool] = None       # 目标审批流是否启用
    created_at: Optional[datetime] = None       # 创建时间
    updated_at: Optional[datetime] = None       # 更新时间



# 规则分页响应
class AdminRuleListResponse(BaseModel):
    """
    管理端规则分页响应模型（与其他列表接口同构）
        :ivar items: 当前页规则列表
        :ivar total: 符合条件的总条数
        :ivar page: 当前页码
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[AdminRuleItem] = []             # 当前页规则列表
    total: int                                  # 总条数
    page: int                                   # 当前页码
    page_size: int                              # 每页条数
    total_pages: int                            # 总页数



# 新建规则入参
class AdminRuleCreate(BaseModel):
    """
    管理端新建规则入参模型
        :ivar name: 规则名称，最长 100（同一类型下建议唯一）
        :ivar rule_type: 规则类型，审批流匹配固定用 "amount"
        :ivar condition: 触发条件的可读描述（必填，便于审计与前端展示）
        :ivar action: 引擎动作：amount 规则固定 assign_chain，check_* 规则固定 check
        :ivar severity: 检查规则风险级别（warn/review/block），amount 规则留空
        :ivar config: 规则参数；amount 类型必须包含 submitter_roles / min_amount / max_amount / flow_id
        :ivar is_active: 是否启用，默认启用
    """
    name: str = Field(..., min_length=1, max_length=100, description="规则名称")        # 必填，最长 100
    rule_type: str = Field("amount", max_length=50, description="规则类型,审批流匹配用 amount")  # 默认 amount
    condition: str = Field(..., min_length=1, description="触发条件描述")               # 必填，给管理员看的说明
    action: str = Field("assign_chain", max_length=50, description="引擎动作")          # 默认 assign_chain
    severity: Optional[str] = Field(None, max_length=20, description="检查规则级别 warn/review/block")
    config: dict = Field(..., description="规则参数(amount 类型必须含 submitter_roles/min_amount/max_amount/flow_id)")  # 必填，JSON 参数
    is_active: bool = Field(True, description="是否启用")                               # 默认启用



# 修改规则入参（PUT 全量替换）
class AdminRuleUpdate(BaseModel):
    """
    管理端修改规则入参模型（PUT 全量语义：字段必须一次发全）
        :ivar name: 规则名称
        :ivar rule_type: 规则类型
        :ivar condition: 触发条件描述
        :ivar action: 引擎动作
        :ivar severity: 检查规则风险级别
        :ivar config: 规则参数
        :ivar is_active: 是否启用（改为启用时会重新校验金额区间是否与其他启用规则重叠）
    """
    name: str = Field(..., min_length=1, max_length=100, description="规则名称")        # 必填，最长 100
    rule_type: str = Field("amount", max_length=50, description="规则类型")               # 默认 amount
    condition: str = Field(..., min_length=1, description="触发条件描述")               # 必填，给管理员看的说明
    action: str = Field("assign_chain", max_length=50, description="引擎动作")          # 默认 assign_chain
    severity: Optional[str] = Field(None, max_length=20, description="检查规则级别 warn/review/block")
    config: dict = Field(..., description="规则参数")                                   # 必填，JSON 参数
    is_active: bool = Field(..., description="是否启用")                                # 必填，全量语义下不可省略
