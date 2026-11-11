"""
用户模块数据模型（注册/登录入参 + 用户信息出参）
    覆盖用户全生命周期的入参校验与出参序列化，包含五类模型：
        - UserBase：用户基础共享字段，作为出参模型的父类，统一定义字段长度约束
        - UserCreate：注册入参，在基础字段上追加明文密码（6~128 位）
        - UserLogin：登录入参，用户名或邮箱 + 密码
        - UserResponse：用户完整信息出参，靠"不声明 password_hash 字段"实现脱敏
        - UserOptionResponse：用户下拉选项精简出参，供人员选择器、筛选器使用，严格过滤隐私字段
    安全约定：所有响应模型都不含 password_hash，由 FastAPI 的 response_model 机制自动裁剪多余字段；
    响应模型统一启用 from_attributes，可直接把 SQLAlchemy 的 User 实例交给 response_model 序列化。
"""



from datetime import datetime
from pydantic import BaseModel, Field
from app.models.user import UserRole   # 复用用户角色枚举
from typing import Optional, List



# 用户共享基础字段
class UserBase(BaseModel):
    """
    用户共享基础字段（注册入参与返回出参共用，统一定义字段长度约束）
        `Field(...)` 表示必填，`Field(None)` 表示可选、默认空；max_length 与数据库列长度保持一致。
        :ivar username: 登录用户名，最长 50，全局唯一（唯一性由接口查重 + 数据库约束共同保证）
        :ivar email: 登录邮箱，最长 100，同样唯一
        :ivar full_name: 真实姓名，可选；但数据库列为非空，注册时应由前端必填
        :ivar phone: 手机号，可选，最长 20
        :ivar department_id: 所属部门ID，可选；employee 角色注册时接口强制校验必填
        :ivar position: 职位，可选
    """
    username: str = Field(..., max_length=50, description="用户名")                    # 必填，最长 50
    email: str = Field(..., max_length=100, description="邮箱")                        # 必填，最长 100
    full_name: Optional[str] = Field(None, max_length=100, description="真实姓名")      # 选填，最长 100
    phone: Optional[str] = Field(None, max_length=20, description="手机号码")           # 选填，最长 20
    department_id: Optional[int] = Field(None, description="所属部门ID")               # 选填；employee 注册时接口强制必填
    position: Optional[str] = Field(None, max_length=100, description="职位")          # 选填，最长 100



# 注册请求
class UserCreate(UserBase):
    """
    注册请求模型（仅入参，响应中永远不会出现明文密码）
        在基础字段之上追加 password；其余角色、激活状态、超级用户标记一律沿用模型默认值，
        不允许前端指定，避免注册接口被用来创建高权限账号。
        :ivar password: 明文密码，长度 6~128，入库前由接口调用 hash_password 转成 bcrypt 密文
    """
    password: str = Field(..., min_length=6, max_length=128, description="明文密码(仅入参,不返回)")  # 必填，6~128 位



# 登录请求
class UserLogin(BaseModel):
    """
    登录请求模型（当前实际登录接口用的是 OAuth2 表单，本模型供 JSON 登录等扩展场景使用）
        :ivar username: 用户名或邮箱，接口内用 or_ 同时匹配两个字段
        :ivar password: 明文密码，登录时与库中密文做 verify 比对
    """
    username: str = Field(..., max_length=50, description="用户名或邮箱")                # 必填，用户名或邮箱
    password: str = Field(..., min_length=6, max_length=128, description="密码")         # 必填，6~128 位



# 返回给前端的用户信息
class UserResponse(UserBase):
    """
    用户信息响应模型（注册成功、用户详情等场景的返回结构）
        脱敏方式：本模型不声明 password_hash 字段，FastAPI 按 response_model 输出时会把多余字段裁掉，
        因此接口可以直接 `return user` 而不必手工组字典。
        :ivar id: 用户ID（数据库主键）
        :ivar role: 角色枚举，序列化为 "employee" 等字符串
        :ivar is_active: 账号是否启用，停用账号不应允许登录
        :ivar created_at: 注册时间
        :ivar last_login_at: 最后登录时间，从未登录过为 null
    """
    id: int                                 # 用户ID
    role: UserRole                          # 角色枚举
    is_active: bool = True                  # 是否激活
    created_at: datetime | None = None      # 注册时间,可空
    last_login_at: datetime | None = None   # 最后登录时间,可空
    ai_consent_at: datetime | None = None   # AI 数据处理授权时间
    ai_consent_version: str | None = None   # AI 数据处理授权版本

    # 允许直接从 ORM 用户对象转换成本响应模型
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性   # 让 Pydantic 能直接读 SQLAlchemy 对象的属性



# AI 数据处理授权请求
class AIConsentRequest(BaseModel):
    """
    用户对"发票影像发送给模型服务"的授权请求
        :ivar consent: True 表示授权，False 表示撤回授权
    """
    consent: bool = Field(..., description="是否同意将发票/凭证影像发送给模型服务")   # 必填



# 用户下拉选项响应模型
class UserOptionResponse(BaseModel):
    """
    用户下拉选项响应模型（人员选择器、筛选器的数据源）
        精简字段设计：只保留"能认出这个人"所需的字段，
        严格过滤密码、邮箱、手机号等隐私信息，降低批量查询用户列表时的泄露风险。
        :ivar id: 用户ID，提交后回传给后端使用
        :ivar username: 用户名，用于兜底展示
        :ivar full_name: 真实姓名，下拉框主要展示字段
        :ivar department_id: 所属部门ID，可用于前端二次分组
        :ivar role: 角色枚举，前端可据此显示角色标签
    """
    id: int                             # 用户ID
    username: str                       # 用户名
    full_name: str                      # 真实姓名
    department_id: int | None = None    # 所属部门ID，可空
    role: UserRole                      # 角色枚举

    # 允许 Pydantic 模型直接从 ORM 对象（SQLAlchemy 的数据库模型实例）读取属性，自动完成字段转换，不需要手动把数据库对象拆成字典
    model_config = {"from_attributes": True}    # 允许直接从 ORM 对象读取属性



# ============ 管理端：用户管理 ============
# 管理端建号入参
class AdminUserCreate(BaseModel):
    """
    管理端建号入参模型（仅 admin 可用）
        为什么不复用公开注册的 UserCreate：这里能指定 role，一旦让注册接口复用，
        就等于开放了"自己注册一个管理员"的提权通道，两个模型必须分开。
        部门字段按角色规则处理（规则实现在 services.auth_service.resolve_department）：
            - admin 不传则默认「应用开发部」
            - boss 必须不传
            - finance 固定「财务部」，传别的部门会报错
            - manager / employee 必填，且部门必须存在并启用
        :ivar username: 登录用户名，最长 50，全局唯一（与邮箱交叉查重）
        :ivar email: 邮箱，最长 100，全局唯一
        :ivar password: 初始明文密码，6~128 位，入库前由服务层 bcrypt 哈希
        :ivar full_name: 真实姓名，必填（数据库 NOT NULL）
        :ivar phone: 手机号，必填（数据库 NOT NULL）
        :ivar position: 职位，选填
        :ivar role: 角色，必填
        :ivar department_id: 所属部门ID，按角色规则可空
    """
    username: str = Field(..., max_length=50, description="登录用户名")                 # 必填，登录用户名
    email: str = Field(..., max_length=100, description="邮箱")                        # 必填，邮箱
    password: str = Field(..., min_length=6, max_length=128, description="初始明文密码(仅入参,不返回)")  # 必填，6~128 位
    full_name: str = Field(..., max_length=100, description="真实姓名")                 # 必填，真实姓名
    phone: str = Field(..., max_length=20, description="手机号码")                     # 必填，手机号
    position: Optional[str] = Field(None, max_length=100, description="职位")          # 选填，职位
    role: UserRole = Field(..., description="用户角色")                               # 必填，管理端可指定
    department_id: Optional[int] = Field(None, description="所属部门ID(按角色规则可空)")  # 选填，按角色规则校验



# 管理端用户列表项
class AdminUserItem(BaseModel):
    """
    管理端用户列表项模型（仅 admin 可访问）
        与 UserOptionResponse 的区别：管理端要能看到账号全貌（含停用账号、邮箱手机、部门名称、最后登录时间），
        但依然不返回 password_hash —— 出参模型不声明该字段，FastAPI 会自动裁掉。
        :ivar id: 用户ID
        :ivar username: 登录用户名
        :ivar full_name: 真实姓名
        :ivar email: 邮箱，管理端用于核对账号
        :ivar phone: 手机号
        :ivar position: 职位，可空
        :ivar role: 角色枚举（admin/boss/finance/manager/employee）
        :ivar department_id: 所属部门ID，boss 等可为空
        :ivar department_name: 所属部门名称，由左连接 departments 表带出，未挂部门时为 null
        :ivar is_active: 账号是否启用（false 即已停用）
        :ivar must_change_password: 是否必须修改密码（管理员建号或重置密码后为 true）
        :ivar created_at: 注册时间
        :ivar last_login_at: 最后登录时间，从未登录过为 null
    """
    id: int                                     # 用户ID
    username: str                               # 登录用户名
    full_name: str                              # 真实姓名
    email: str                                  # 邮箱
    phone: str                                  # 手机号
    position: str | None = None                 # 职位，可空
    role: UserRole                              # 角色枚举
    department_id: int | None = None            # 所属部门ID，可空
    department_name: str | None = None          # 所属部门名称，可空
    is_active: bool                             # 账号是否启用
    must_change_password: bool = False          # 是否必须修改密码(管理员建号/重置密码后为 True)
    created_at: datetime | None = None          # 注册时间，可空
    last_login_at: datetime | None = None       # 最后登录时间，可空



# 管理端用户分页响应
class AdminUserListResponse(BaseModel):
    """
    管理端用户分页响应模型
        与报销单等列表接口保持同构（items/total/page/page_size/total_pages），前端可复用同一套分页组件。
        :ivar items: 当前页用户列表
        :ivar total: 符合条件的总条数（注意是过滤后的总数，不是全表用户数）
        :ivar page: 当前页码，从 1 开始
        :ivar page_size: 每页条数
        :ivar total_pages: 总页数
    """
    items: List[AdminUserItem] = []     # 当前页用户列表
    total: int                          # 符合条件的总条数
    page: int                           # 当前页码
    page_size: int                      # 每页条数
    total_pages: int                    # 总页数



# 管理端用户详情
class AdminUserDetail(AdminUserItem):
    """
    管理端用户详情模型（编辑页回填 + 风险操作前置提示）
        在列表项字段之上追加"编辑/停用前必须知道"的判断信息。
        这些字段放列表里代价太大（每行都要算待办、查负责人），放详情里只在打开编辑页时算一次。
        :ivar is_department_manager: 是否某个部门的负责人（departments.manager_id 指向他）
        :ivar managed_department_id: 负责的部门ID，不是负责人时为 null
        :ivar managed_department_name: 负责的部门名称，不是负责人时为 null
        :ivar pending_approval_count: 当前有多少条待办正等着他审批（停用/降级前用来拦截）
        :ivar is_last_active_admin: 是否为系统里唯一一个启用状态的管理员（为 true 时禁止停用/降级）
    """
    is_department_manager: bool = False             # 是否为部门负责人
    managed_department_id: int | None = None        # 负责的部门ID
    managed_department_name: str | None = None      # 负责的部门名称
    pending_approval_count: int = 0                 # 待他审批的单据数
    is_last_active_admin: bool = False              # 是否最后一个启用的 admin



# 管理端改用户入参（PUT 全量替换语义）
class AdminUserUpdate(BaseModel):
    """
    管理端修改用户入参模型（PUT 全量替换语义，所有字段一次发全）
        为什么用 PUT 而不是 PATCH：前端编辑页本来就从详情接口回填了整张表单，
        用 PUT 可以要求"字段必须发全"，避免"只传了 is_active，结果把姓名/手机清空"这类部分更新的坑。
        不可修改的字段（不在本模型里，传了也会被忽略/报错）：
            - username：登录凭据兼审计线索，改名会让历史记录对不上人；
            - password：走单独的重置密码接口，绝不混在资料更新里。
        :ivar email: 邮箱，必填且不能与其他人重复
        :ivar full_name: 真实姓名，必填
        :ivar phone: 手机号，必填
        :ivar position: 职位，选填
        :ivar role: 角色，必填；改角色会联动维护部门负责人
        :ivar department_id: 所属部门ID，按角色规则校验
        :ivar is_active: 是否启用，必填（全量语义下没传就无法判断是要启用还是停用）
    """
    email: str = Field(..., max_length=100, description="邮箱")                        # 必填，邮箱
    full_name: str = Field(..., max_length=100, description="真实姓名")                 # 必填，真实姓名
    phone: str = Field(..., max_length=20, description="手机号码")                     # 必填，手机号
    position: Optional[str] = Field(None, max_length=100, description="职位")          # 选填，职位
    role: UserRole = Field(..., description="用户角色")                               # 必填，改角色会联动部门负责人
    department_id: Optional[int] = Field(None, description="所属部门ID(按角色规则可空)")  # 选填，按角色规则校验
    is_active: bool = Field(..., description="是否启用")                              # 必填，全量语义下不可省略



# 管理端重置密码入参
class AdminPasswordReset(BaseModel):
    """
    管理端重置密码入参模型（仅 admin 可用）
        语义：管理员直接给用户设一个新密码，不校验旧密码；
        重置成功后 must_change_password 会被置 True，用户下次登录必须先改掉这个"别人设的密码"。
        :ivar new_password: 新密码，6~128 位，由服务层 bcrypt 哈希后入库
    """
    new_password: str = Field(..., min_length=6, max_length=128, description="新密码(6~128位)")  # 必填，6~128 位



# 修改密码入参（用户自己改）
class ChangePassword(BaseModel):
    """
    修改密码入参模型（登录用户自己改密码，必须提供旧密码）
        两个用途：
            - 常规改密：用户想换密码时主动调用；
            - 强制改密：管理员建号/重置密码后 must_change_password=True，
              此时除本接口外的业务接口都会被拦（403），必须先在这里改掉初始密码。
        :ivar old_password: 旧密码（当前的明文密码），用于确认是本人操作
        :ivar new_password: 新密码，6~128 位，且不能与旧密码相同
    """
    old_password: str = Field(..., min_length=6, max_length=128, description="旧密码")              # 必填，用于确认本人
    new_password: str = Field(..., min_length=6, max_length=128, description="新密码(6~128位,不能与旧密码相同)")  # 必填，不能与旧密码相同



# 修改密码响应
class ChangePasswordResponse(BaseModel):
    """
    修改密码响应模型
        改密成功后 token_version 会 +1，该账号所有旧令牌立即失效；
        为了不让"刚改完密码的人被自己的下一次请求踢下线"，这里直接下发一张新令牌。
        :ivar user: 更新后的用户信息（must_change_password 已为 false）
        :ivar access_token: 用新 token_version 重新签发的访问令牌
        :ivar token_type: 固定为 bearer，前端按 Authorization: Bearer <token> 使用
    """
    user: UserResponse               # 更新后的用户信息
    access_token: str                # 新签发的访问令牌
    token_type: str = "bearer"       # 令牌类型，固定 bearer
