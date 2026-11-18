"""
用户数据模型（users 表 + 用户角色枚举）
    系统身份、组织归属与功能权限的基础表，是登录鉴权、数据隔离、审批资格判定的起点：
        - 账号安全：username / email 全局唯一；password_hash 只存 bcrypt 密文，永不存明文
        - 组织归属：department_id 指向 departments，boss 可为空，其余角色由注册接口强制校验必填
        - 角色权限：role 取 UserRole 枚举（admin / boss / finance / manager / employee），
          配合 has_permission() 完成功能级授权，配合审批流的 role 字符串完成审批资格判定
        - 账号状态：is_active 控制启停（停用后不应允许登录），is_superuser 预留给系统级超级权限
        - 凭证版本：token_version 写入 JWT，改密/重置密码时 +1，使旧令牌立即失效
    关系：User.expenses（本人提交的报销单）、User.approvals（本人作为审批人的审批记录），均为 dynamic 惰性查询。
    注意：枚举取值会同时进入数据库、JWT 之外的角色映射与审批流配置，修改枚举值时需同步检查
    has_permission 的权限映射表与 approval_flows.levels 中的 role 字符串。
"""


import enum
from app.models.base import Base
from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, Enum, ForeignKey, Integer
from sqlalchemy.orm import relationship



# 用户角色枚举 --- `enum.Enum`:枚举能力限定角色只能取这五个值，防止乱写
class UserRole(str, enum.Enum):
    """
    用户角色枚举（系统唯一的角色取值来源）
    `str, enum.Enum`：让枚举值同时是字符串，存进 MySQL、接口返回、拼 SQL 条件都方便
    （否则默认是 `UserRole.ADMIN` 这种对象，序列化时还需要额外转换）。
    `成员名 = "值"`：左边 ADMIN 是代码里用的名字，右边 "admin" 才是真正存库、参与比较的字符串。
    取值约定：
        - 必须写成 `ADMIN = "admin"`，注意不能多写逗号，否则会变成元组 ("admin",) 而不是字符串；
        - 成员顺序即前端角色字典的返回顺序；
        - 角色到权限的映射见 User.has_permission()，审批流配置中的 role 字段也是这套字符串。
    """
    ADMIN = "admin",            # 系统管理员
    BOSS = "boss",              # 公司老板
    FINANCE = "finance",        # 财务人员
    MANAGER = "manager",        # 部门经理
    EMPLOYEE = "employee",      # 普通员工



# 用户表结构
class User(Base):
    """
    用户表模型：一个账号一条记录，承载登录凭证、组织归属、角色与账号状态
        字段分组：
            - 主键与外键：id、department_id（关联部门，boss 可为空）
            - 账号信息：username / email（唯一且建索引）、password_hash（密文）
            - 个人信息：full_name / phone / position
            - 权限状态：role（枚举）、is_active（是否启用）、is_superuser（是否超级用户）
            - 改密标记：must_change_password（管理员设的初始密码必须由本人改掉才能继续使用）
            - AI 授权：ai_consent_at / ai_consent_version（发票影像发送给模型前的显式授权）
            - 时间：last_login_at（登录接口维护）、created_at / updated_at（Base 提供）
        行为：
            - is_finance / is_manager：便捷属性，直接读角色，避免外部写重复判断
            - has_permission()：角色到功能权限的唯一映射入口
            - expenses / approvals：ORM 导航关系，不产生新字段
    """
    # 表名(显示写出,方便阅读)
    __tablename__ = "users"             # 表名：用户


    # ===== 主键 =====
    id = Column(
        BigInteger,              # 大整数列类型,用来存整数
        primary_key=True,        # 主键
        index=True,              # 建索引，查得快
        autoincrement=True,      # 自增,不用手动传id
        comment="用户ID"          # 注释
    )

    # ===== 外键:关联部门表 =====
    # department_id:所属部门ID,外键→departments.id;boss 可空,其余角色必填(应用层校验)
    department_id = Column(
        BigInteger,                     # 字段类型:大整数,和 departments.id 一致
        ForeignKey("departments.id"),   # 外键:关联 departments 表的 id
        nullable=True,                  # 可空:boss 不挂部门;其余角色由接口/注册校验必填
        index=True,                     # 建立索引,按 department_id 查询更快
        comment="所属部门ID"             # 字段备注
    )

    # ===== 账号基本信息 =====
    # username：登录用户名（唯一）
    username = Column(
        String(50),             # 字符串类型，最长 50 个字符
        unique=True,            # 唯一约束：用户名不能重复
        nullable=False,         # 非空：注册必须填用户名
        index=True,             # 建索引：按用户名查询更快
        comment="登录用户名"      # 字段备注，写进数据库
    )

    # email：登录邮箱（唯一，可用于找回账号）
    email = Column(
        String(100),            # 字符串类型，最长 100 个字符
        unique=True,            # 唯一约束：邮箱不能重复
        nullable=False,         # 非空：注册必须填邮箱
        index=True,             # 建索引：按邮箱查询更快
        comment="登录邮箱"        # 字段备注，写进数据库
    )

    # password_hash：密码的哈希值，绝不存明文密码
    password_hash = Column(
        String(255),                    # # 字符串类型，哈希结果较长，给 255 个字符
        nullable=False,                 # 非空：注册必须填密码
        comment="密码哈希(不存明文)"      # 字段备注，写进数据库
    )


    # ===== 个人信息 =====
    # full_name：真实姓名
    full_name = Column(
        String(100),             # 字符串类型，真实姓名,最长 100 个字符
        nullable=False,          # 非空：注册必须填真实姓名
        comment="真实姓名"        # 字段备注，写进数据库
    )

    # phone:手机号码
    phone = Column(
        String(20),              # 字符串类型，手机号码,最长 20 个字符
        nullable=False,          # 非空：注册必须填手机号码
        comment="手机号码"        # 字段备注，写进数据库
    )

    # position:职位
    position = Column(
        String(100),             # 字符串类型，职位,最长 100 个字符
        nullable=True,           # 可空
        comment="职位"            # 字段备注，写进数据库
    )


    # ===== 角色和权限 =====
    # role：用户角色，只能取 UserRole 枚举里的四个值；默认普通员工
    role = Column(
        Enum(
            UserRole,               # 枚举类型，限定只能选 UserRole 枚举里的值
            values_callable=lambda enum_cls: [m.value for m in enum_cls], # 存 .value（小写），而非成员名大写
            native_enum=True,       # 与 schema.sql 的 MySQL ENUM 保持一致
            name="user_role",       # 数据库枚举类型名
        ),
        nullable=False,             # 非空：必须填角色
        default=UserRole.EMPLOYEE,  # 默认值：普通员工
        comment="用户角色"           # 字段备注，写进数据库
    )


    # ===== 账号状态 =====
    # is_active：账号是否激活（False 可冻结账号，禁止登录）
    is_active = Column(
        Boolean,                # 布尔类型：True 或 False
        nullable=False,         # 非空：必须填
        default=True,           # 默认值：True 默认激活
        comment="账号是否激活"    # 字段备注，写进数据库
    )

    # is_superuser:是否为超级用户(系统最高权限,一般不用)
    is_superuser = Column(
        Boolean,                # 布尔类型：True 或 False
        nullable=False,         # 非空：必须填
        default=False,          # 默认值：False 默认不是超级用户
        comment="是否为超级用户"  # 字段备注，写进数据库
    )

    # must_change_password:是否必须修改密码
    # 管理员建号/重置密码后置 True：此时密码是"别人设的"，用户首次登录必须自己改掉才能用其他接口
    must_change_password = Column(
        Boolean,                    # 布尔类型：True 或 False
        nullable=False,             # 非空：必须填
        default=False,              # 默认值：False(自己注册的账号不需要强制改密)
        comment="是否必须修改密码"    # 字段备注，写进数据库
    )

    # token_version:令牌版本号
    # 签发 JWT 时写进载荷，鉴权时与库中值比对；改密/重置密码时 +1，旧令牌立刻全部失效
    token_version = Column(
        Integer,                    # 整数类型：每次改密自增 1
        nullable=False,             # 非空：必须填
        default=0,                  # 默认值：0(新账号)
        comment="令牌版本号(改密+1,旧令牌失效)"  # 字段备注，写进数据库
    )

    # ===== AI 数据处理授权 =====
    # 发票影像会发给模型服务，必须由用户显式授权；未授权时跳过凭证识别
    ai_consent_at = Column(
        DateTime(timezone=True),               # 授权时间
        nullable=True,                         # 可空：未授权为 NULL
        comment="AI 数据处理授权时间"           # 字段备注
    )

    # 授权条款版本：条款升级后版本变化，需要用户重新授权
    ai_consent_version = Column(
        String(20),                            # 版本号，如 v1
        nullable=True,                         # 可空：未授权为 NULL
        comment="AI 数据处理授权版本"           # 字段备注
    )


    # ===== 时间戳 =====
    # last_login_at：最后登录时间（基类没有，单独写；可空，没登录过就是 NULL）
    last_login_at = Column(
        DateTime(timezone=True),               # 日期时间类型,timezone=True 带时区
        nullable=True,                         # 可空：没登录过就是 NULL
        comment="最后登录时间"                   # 字段备注，写进数据库
    )

    # created_at 记录创建时间 / updated_at 记录最后修改时间 已经在 base.py里写好


    # ===== 便捷判断属性 =====
    """
    - ADMIN 管理员/ BOSS 老板：一般靠 "是否超级用户 / 直接放行" 处理，不需要单独的 `is_admin` 布尔；
    - EMPLOYEE 普通员工：是默认角色，判断 "是不是员工" 意义不大，多数情况直接用 `role` 或后面的 `has_permission`。
    """
    # 是否财务人员：把方法变成属性，外面用 user.is_finance 直接取，不用加括号
    @property
    def is_finance(self) -> bool:
        """
        是否为财务人员（便捷属性，外部直接 `user.is_finance` 读取，不用加括号）
            :return: role 为 finance 时返回 True
        """
        return self.role == UserRole.FINANCE

    # 是否部门经理
    @property
    def is_manager(self) -> bool:
        """
        是否为部门经理（便捷属性，外部直接 `user.is_manager` 读取，不用加括号）
            :return: role 为 manager 时返回 True
        """
        return self.role == UserRole.MANAGER


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 一个用户提交的所有报销单；dynamic 返回查询器，用到时才加载
    expenses = relationship(
        "Expense",        # 关联的目标模型类名（用字符串，避免循环导入）
        back_populates="user",      # 配对 Expense.user（报销单 → 提交人），双向对应
        lazy="dynamic",             # 懒加载：访问 user.expenses 时才查库，返回查询器
    )

    # 一个用户作为审批人留下的所有审批记录
    approvals = relationship(
        "Approval",       # 关联的目标模型类名（用字符串，避免循环导入）
        back_populates="approver",  # 配对 Approval.approver（审批记录 → 审批人）
        lazy="dynamic",             # 懒加载：访问 user.approvals 时才查库，返回查询器
    )


    # ===== 调试打印显示直观信息 =====
    # 调试打印用：__repr__ 是 Python 的 "对象自我描述" 方法，决定这个对象被 print、在调试器里查看、放进列表打印时显示成什么字符串
    def __repr__(self) -> str:
        """
        调试打印用：决定该对象被 print()、在调试器里查看或在列表中打印时的显示内容
            :return: 形如 <User(id=1,username=zhangsan,role=UserRole.EMPLOYEE)>，只暴露定位信息，不含密码
        """
        return f"<User(id={self.id},username={self.username},role={self.role})>"


    # ===== 检查用户是否有指定权限 =====
    def has_permission(self,permission: str) -> bool:
        """
        检查用户是否拥有指定的功能权限（全系统功能级授权的唯一入口）
            设计说明：权限在这里用"角色 → 权限字符串列表"的静态映射实现，
            比在每个接口里散写 `role == ...` 更集中，也便于后续替换成数据库权限表。
            权限字符串约定：
                - 基础：submit（提交）、view_own（看自己）、edit_own（改自己）、cancel（撤销）
                - 审批：approve（可审批）、reject（可驳回）、view_department（看本部门）
                - 管理：view_all（看全公司，含他人单据的审批轨迹）、pay（财务支付）、export（导出）、
                  manage_user（管理端用户管理：建号/改角色/启停）
            角色定位：admin 只做基础数据与全局查看/用户管理（view_all + manage_user），
            刻意不授予 approve / pay，即"能查所有单据和审批记录、能管账号，但不参与审批、不做付款"。
            典型调用：审批接口 `current_user.has_permission("approve")`，支付接口 `has_permission("pay")`。
            注意：本方法只做功能级判断，数据级归属（如"只能看自己提交的单"）仍需接口内单独过滤。
            :param permission: 待判断的权限字符串，取值见上面的约定
            :return: 拥有该权限返回 True，否则返回 False（角色不存在时也返回 False）
        """
        # 1.角色 -> 权限列表 的映射表
        permission_map = {
            "employee": ["submit","view_own","edit_own","cancel"],  # 员工：提交/看自己/改自己/撤销
            "manager": ["submit","view_own","edit_own","cancel",
                "approve", "reject", "view_department"],            # 经理：基础员工权 + 审批/拒绝/看本部门
            "finance":["submit","view_own","edit_own","cancel",
                "approve","reject","view_all","pay","export"],      # 财务：基础员工权 + 审批/拒绝/看全部/支付/导出
            "boss": ["submit", "view_own", "edit_own", "cancel",
                "approve", "reject", "view_all"],                    # 老板：基础员工权 + 审批/拒绝/看全部
            "admin": ["submit", "view_own", "edit_own", "cancel",
                      "view_all", "export", "manage_user"],           # 管理员：基础员工权 + 看全部/导出/管用户；刻意不含 approve/pay，admin 只查不批不付款
        }

        # 2.获取用户角色对应的权限列表
        # self.role.value:当前用户的角色字符串，如 `"manager"`，作为查找的键; []:字典里找不到这个角色时，返回空列表 `[]`
        role_permission = permission_map.get(self.role.value,[])    # 该角色拥有的权限列表

        # 3.判断目标权限是否在该角色的权限列表里
        return permission in role_permission


