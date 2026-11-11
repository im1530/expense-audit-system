"""
部门模块数据模型
    分为两类：
        - 业务侧出参（DepartmentResponse）：给"选部门"下拉框用，只返回 id/名称/负责人/备注，只含启用部门；
        - 管理端模型（Admin*）：给管理端部门维护用，含停用状态、成员数、负责人姓名等管理信息，
          以及新建/修改的入参模型。
    所有出参模型都启用 from_attributes，可把 SQLAlchemy 的 Department 实例直接交给 response_model 序列化。
"""



from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# 部门下拉选项响应模型
class DepartmentResponse(BaseModel):
    """
    部门下拉选项响应模型（部门列表接口的返回结构，与 departments 表字段严格对齐）
        用于注册表单的「所属部门」选择、以及各类列表页的部门筛选器；
        只返回选择器所需的字段，不暴露 is_active 等内部管理标记（列表已按启用状态过滤）。
        :ivar id: 部门ID，提交给后端使用
        :ivar name: 部门名称，下拉框展示文本
        :ivar manager_id: 部门负责人（经理）用户ID，未配置时为 null
        :ivar description: 部门备注，可用于下拉项的辅助说明
    """
    id: int                             # 部门ID
    name: str                           # 部门名称
    manager_id: int | None = None       # 部门负责人ID，可空
    description: str | None = None      # 部门备注，可空

    # 允许 Pydantic 模型直接从 ORM 对象（SQLAlchemy 的数据库模型实例）读取属性，自动完成字段转换，不需要手动把数据库对象拆成字典
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性



# ============ 管理端：部门管理 ============
# 管理端部门列表项
class AdminDepartmentItem(BaseModel):
    """
    管理端部门列表项模型（仅 admin 可访问）
        与 DepartmentResponse 的区别：管理端要能看到停用部门、负责人姓名与成员数，
        成员数是"能不能停用这个部门"的直接依据（还有启用员工时不允许停用）。
        :ivar id: 部门ID
        :ivar name: 部门名称（全局唯一）
        :ivar description: 部门备注，可空
        :ivar manager_id: 负责人用户ID，未配置时为 null
        :ivar manager_name: 负责人真实姓名，由左连接 users 表带出
        :ivar is_active: 部门是否启用
        :ivar user_count: 该部门下的全部成员数（含停用账号）
        :ivar active_user_count: 该部门下启用状态的成员数（大于 0 时不允许停用部门）
        :ivar created_at: 创建时间
    """
    id: int                                     # 部门ID
    name: str                                   # 部门名称
    description: str | None = None              # 部门备注，可空
    manager_id: int | None = None               # 负责人用户ID，可空
    manager_name: str | None = None             # 负责人姓名，可空
    is_active: bool                             # 部门是否启用
    user_count: int = 0                         # 全部成员数
    active_user_count: int = 0                  # 启用状态成员数
    created_at: datetime | None = None          # 创建时间



# 管理端部门详情
class AdminDepartmentDetail(AdminDepartmentItem):
    """
    管理端部门详情模型（编辑页回填 + 停用前风险提示）
        :ivar pending_expense_count: 该部门成员提交、仍处于待审批状态的单据数（大于 0 时不允许停用）
        :ivar is_reserved: 是否为系统保留部门（应用开发部 / 财务部），保留部门不允许改名或停用
    """
    pending_expense_count: int = 0              # 待审批单据数（本部门成员提交的）
    is_reserved: bool = False                   # 是否系统保留部门



# 管理端部门分页响应
class AdminDepartmentListResponse(BaseModel):
    """
    管理端部门分页响应模型（与其他列表接口同构，前端可复用分页组件）
        :ivar items: 当前页部门列表
        :ivar total: 符合条件的总条数
        :ivar page: 当前页码，从 1 开始
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[AdminDepartmentItem] = []       # 当前页部门列表
    total: int                                  # 符合条件的总条数
    page: int                                   # 当前页码
    page_size: int                              # 每页条数
    total_pages: int                            # 总页数



# 管理端新建部门入参
class AdminDepartmentCreate(BaseModel):
    """
    管理端新建部门入参模型（仅 admin 可用）
        新建的部门默认启用、默认没有负责人——负责人请通过"用户管理"把某位 manager 放到该部门，
        或后续用 PUT 指定（负责人必须是本部门的 manager 角色用户）。
        :ivar name: 部门名称，最长 50，全局唯一
        :ivar description: 部门备注，最长 200，选填
    """
    name: str = Field(..., min_length=1, max_length=50, description="部门名称(全局唯一)")  # 必填，全局唯一
    description: Optional[str] = Field(None, max_length=200, description="部门备注")        # 选填，最长 200



# 管理端修改部门入参（PUT 全量替换语义）
class AdminDepartmentUpdate(BaseModel):
    """
    管理端修改部门入参模型（PUT 全量替换：字段必须一次发全）
        为什么用 PUT：前端编辑页从详情接口回填整张表单，全量提交可以避免"只改备注却把负责人清空"。
        :ivar name: 部门名称，全局唯一；系统保留部门（应用开发部/财务部）不允许改名
        :ivar description: 部门备注，选填
        :ivar is_active: 是否启用；停用前要求"没有启用成员、没有待审批单据"
        :ivar manager_id: 负责人用户ID；只能指向本部门的 manager 角色启用用户，传 null 表示清空负责人
    """
    name: str = Field(..., min_length=1, max_length=50, description="部门名称(全局唯一)")  # 必填，保留部门不允许改名
    description: Optional[str] = Field(None, max_length=200, description="部门备注")        # 选填，最长 200
    is_active: bool = Field(..., description="是否启用")                                    # 必填，停用前校验成员/待审批
    manager_id: Optional[int] = Field(None, description="负责人用户ID(必须是本部门的 manager 角色用户)")  # 选填，null 表示清空负责人
