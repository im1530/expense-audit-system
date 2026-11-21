"""
审批模块数据模型（审批入参 + 审批记录出参 + 审批轨迹展示项 + 审批流管理）
    覆盖审批操作入参与审批记录出参全场景，包含三类业务模型：
        - ApprovalAction：审批动作入参，承载审批结果与审批意见，用于审批执行接口的参数校验
        - ApprovalResponse：标准审批记录出参，与 approvals 表字段严格对齐，作为审批接口的返回值
        - ApprovalRecordItem：审批轨迹展示项，在记录基础上扩展审批人姓名、角色，供报销单详情页的时间线渲染
    另外包含管理端的审批流模型（FlowLevel / AdminFlow*），用于维护 approval_flows 表的模板定义。
    响应模型均启用 from_attributes，支持把 SQLAlchemy 实体直接交给 response_model 序列化。
    取值约束：ApprovalAction.status 复用 ApprovalStatus 枚举（与数据库列同一套取值），
    但审批结论在业务上只能是 approved（通过）或 rejected（驳回），该模型用校验器把范围收窄，
    pending / recalled / cancelled / obsoleted 等"记录状态"一律 422 拒绝。
"""



from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from app.models import UserRole
from app.models.approval import ApprovalStatus



# 审批动作
class ApprovalAction(BaseModel):
    """
    审批动作入参模型（审批人对某张报销单提交"通过/驳回"时的请求体）
        审批结论只接受 approved / rejected 两个值：虽然字段类型复用 ApprovalStatus（与库表同源），
        但 pending 等状态表示"记录尚未被处理"，不能当作审批结论，由下面的校验器兜住。
        :ivar status: 审批结论，仅 approved（通过）或 rejected（驳回）
        :ivar comment: 审批意见，选填，最长 500 字（如"同意"、"票据不全，退回"）
    """
    status:ApprovalStatus = Field(...,description="审批结论:approved通过/rejected驳回")  # 必填：仅通过/驳回，其余值由校验器拦截
    comment:Optional[str] = Field(None, max_length=500,description="审批意见,可空")      # 选填：审批意见，最长 500 字

    # 校验审批结论只能是"通过"或"驳回",其余枚举值(待审批/已撤回/已撤销/已作废)一律拒绝
    @field_validator("status")      # 声明：下面这个函数用来校验 status 字段
    @classmethod                    # 类方法，Pydantic v2 要求这样写，不用创建实例
    def validate_status(cls, v: ApprovalStatus) -> ApprovalStatus:
        """
        收窄审批结论的取值范围：只允许 approved / rejected
            校验失败抛 ValueError，Pydantic 会把它包装成 422 并指明是 status 字段的问题，
            请求不会进入审批接口的业务逻辑。
            :param v: 前端传入的审批结论（已由 Pydantic 转成 ApprovalStatus 枚举）
            :return: 校验通过时原样返回，交由 Pydantic 继续组装模型
            :raises ValueError: 传入 pending / recalled / cancelled / obsoleted 等非审批结论值
        """
        # 1.只接受"通过/驳回"两种审批结论
        if v not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            raise ValueError("审批结论只能是 approved(通过) 或 rejected(驳回)")
        # 2.校验通过，原样返回
        return v



# 返回给前端的审批记录
class ApprovalResponse(BaseModel):
    """
    审批记录响应模型（审批执行接口的直接返回值，与 approvals 表字段对齐）
        :ivar id: 审批记录ID
        :ivar expense_id: 所属报销单ID
        :ivar approver_id: 审批人ID；提交时生成的待办记录还未被处理，此时为 null
        :ivar flow_id: 所属审批流ID
        :ivar status: 该条记录的状态（approved / rejected / pending / recalled / cancelled / obsoleted）
        :ivar level: 产生该记录的审批层级
        :ivar comment: 审批意见或系统备注
        :ivar created_at: 记录创建时间，即审批动作发生时间
    """
    id: int                         # 审批记录ID
    expense_id: int                 # 所属报销单ID
    approver_id: Optional[int]      # 审批人ID(待审时为空)
    flow_id: Optional[int]          # 所属审批流ID
    status:ApprovalStatus           # 审批状态
    level:Optional[int]             # 第几级审批
    comment:Optional[str]           # 审批意见
    created_at:datetime             # 任务创建时间（提交/进入本级的时间）
    acted_at:Optional[datetime]     # 处理时间（审批/撤回/撤销时回填）

    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性



# 审批历史记录项模型
class ApprovalRecordItem(BaseModel):
    """
    审批轨迹展示项模型（报销单详情页的审批时间线，由审批历史接口联表组装后返回）
        相比 ApprovalResponse，额外携带审批人姓名与角色，便于前端直接渲染"谁、以什么身份、在第几级做了什么"。
        :ivar id: 审批记录ID
        :ivar expense_id: 所属报销单ID
        :ivar approver_id: 审批人用户ID；提交时生成的待办记录尚未被处理，此时为 null
        :ivar approver_name: 审批人真实姓名，来自 users.full_name；待处理记录为 null
        :ivar approver_role: 审批人角色枚举，来自 users.role；待处理记录为 null
        :ivar flow_id: 所属审批流ID
        :ivar level: 第几级审批
        :ivar status: 该条记录的状态（approved / rejected / pending / recalled / cancelled / obsoleted）
        :ivar comment: 审批意见或系统备注（可空：待处理记录可能是系统写的说明）
        :ivar created_at: 审批执行时间，用于时间线排序展示
    """
    id: int                             # 审批记录主键ID
    expense_id: int                     # 所属报销单ID
    approver_id: Optional[int]          # 审批人用户ID(待审时为空)
    approver_name: Optional[str]        # 审批人真实姓名(待审时为空)
    approver_role: Optional[UserRole]   # 审批人角色(全局枚举,待审时为空)
    flow_id: int                        # 所属审批流ID
    level: int                          # 第几级审批
    status: ApprovalStatus              # 审批结果(枚举)
    comment: Optional[str]              # 审批意见(唯一可空字段,选填)
    created_at: datetime                # 任务创建时间（提交/进入本级的时间）
    acted_at: Optional[datetime]        # 处理时间（审批/撤回/撤销时回填）

    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性



# ============ 管理端：审批流管理 ============
# 审批流层级项
class FlowLevel(BaseModel):
    """
    审批流层级项（levels 数组里的一项）
        含义：第 level 级由 role 这个角色来审批。
        为什么 role 不用 UserRole 枚举：审批流的角色集合比用户角色少一个取值、又多一个取值——
            - 支持 manager / finance / boss；
            - 额外支持 finance_other（"除申请人本人外的任一财务"，用户角色里没有这一项）；
            - 不支持 admin（admin 没有 approve 权限，若出现在审批流里会让单据永久卡在该级）。
        :ivar level: 审批层级，必须从 1 开始连续递增
        :ivar role: 本级要求的审批角色
    """
    level: int = Field(..., ge=1, description="审批层级,从1开始连续")                    # 必填：从 1 开始，逐级递增
    role: str = Field(..., description="本级审批角色:manager/finance_other/finance/boss")  # 必填：本级由谁审批



# 管理端审批流列表项
class AdminFlowItem(BaseModel):
    """
    管理端审批流出参模型
        :ivar id: 审批流ID
        :ivar name: 流程名称
        :ivar description: 流程说明
        :ivar levels: 各级审批配置
        :ivar is_active: 是否启用（停用后不能被匹配到）
        :ivar is_default: 是否为默认流程（全表至多一条，规则都不命中时用它兜底）
        :ivar expense_count: 有多少张报销单绑定了这个流程（大于 0 时不允许删除）
        :ivar rule_count: 有多少条规则引用了这个流程（大于 0 时不允许删除/停用）
        :ivar created_at: 创建时间
    """
    id: int                                     # 审批流ID
    name: str                                   # 流程名称
    description: Optional[str] = None           # 流程说明
    levels: List[FlowLevel] = []                # 各级审批配置
    is_active: bool                             # 是否启用
    is_default: bool                            # 是否默认流程
    expense_count: int = 0                      # 绑定该流程的单据数
    rule_count: int = 0                         # 引用该流程的规则数
    created_at: Optional[datetime] = None       # 创建时间



# 管理端审批流分页响应
class AdminFlowListResponse(BaseModel):
    """
    管理端审批流分页响应模型（与其他列表接口同构）
        :ivar items: 当前页审批流列表
        :ivar total: 符合条件的总条数
        :ivar page: 当前页码
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[AdminFlowItem] = []             # 当前页审批流列表
    total: int                                  # 总条数
    page: int                                   # 当前页码
    page_size: int                              # 每页条数
    total_pages: int                            # 总页数



# 新建审批流入参
class AdminFlowCreate(BaseModel):
    """
    管理端新建审批流入参模型
        :ivar name: 流程名称，最长 100，全局唯一
        :ivar description: 流程说明，选填
        :ivar levels: 各级审批配置，至少一级，level 必须从 1 连续递增
        :ivar is_active: 是否启用，默认启用
        :ivar is_default: 是否设为默认流程；设为 true 会自动把其他流程的默认标记清掉
    """
    name: str = Field(..., min_length=1, max_length=100, description="流程名称(全局唯一)")  # 必填：流程名称，全局唯一
    description: Optional[str] = Field(None, description="流程说明")                       # 选填：流程说明
    levels: List[FlowLevel] = Field(..., min_length=1, description="各级审批配置,至少一级")  # 必填：至少一级
    is_active: bool = Field(True, description="是否启用")                                   # 选填：默认启用
    is_default: bool = Field(False, description="是否设为默认流程")                          # 选填：默认 false，设为 true 会清掉其他默认流



# 修改审批流入参（PUT 全量替换）
class AdminFlowUpdate(BaseModel):
    """
    管理端修改审批流入参模型（PUT 全量语义）
        :ivar name: 流程名称
        :ivar description: 流程说明
        :ivar levels: 各级审批配置
        :ivar is_active: 是否启用（默认流程不允许直接停用；被启用规则引用时也不允许停用）
        :ivar is_default: 是否设为默认流程
    """
    name: str = Field(..., min_length=1, max_length=100, description="流程名称")        # 必填：流程名称
    description: Optional[str] = Field(None, description="流程说明")                    # 选填：流程说明
    levels: List[FlowLevel] = Field(..., min_length=1, description="各级审批配置,至少一级")  # 必填：至少一级
    is_active: bool = Field(..., description="是否启用")                                # 必填：是否启用
    is_default: bool = Field(..., description="是否设为默认流程")                        # 必填：是否默认流程
