"""
部门数据模型（departments 表）
    记录部门基本信息及其负责人（经理），是"同部门审批"与"部门数据隔离"的组织基础：
        - manager_id：指向 users.id，标记该部门由谁审批本部门员工的报销单（可为空，表示暂未配置）
        - is_active：部门是否启用，停用的部门不应再出现在注册、筛选等下拉选项中
        - manager：ORM 关系属性，访问 department.manager 即可取到负责人用户对象
    注意：本表与 users.department_id 互为外键，属于同一组循环引用，因此两边都用字符串形式声明关系，
    并通过 foreign_keys 明确指向，避免 SQLAlchemy 推断关系时产生歧义。
"""



from datetime import datetime
from app.models.base import Base
from sqlalchemy import Column, BigInteger, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship


# 部门表
class Department(Base):
    """
    部门表模型：一个部门一条记录，至多配置一名负责人（经理）
        职责：既是组织架构的载体，也是"本部门审批"判定与"按部门筛选"的关联依据。
        - 唯一性/完整性：name 非空；manager_id 可为空（部门可以暂时没有负责人）
        - 软删除：用 is_active 标记停用，不物理删除，避免历史报销单的部门归属丢失
        - 时间字段：created_at / updated_at 由 Base 统一提供
    """
    # 显式指定表名
    __tablename__ = "departments"           # 表名：部门


    # ===== 主键 =====
    id = Column(
        BigInteger,                 # 字段类型:大整数,适合做主键自增
        primary_key=True,           # 主键
        index=True,                 # 建立索引,按 id 查询更快
        autoincrement=True,         # 自增,不用手动传 id
        comment="部门ID"             # 字段备注,写进数据库
    )


    # ===== 外键:关联用户表(部门负责人/经理) =====
    manager_id = Column(
        BigInteger,                 # 字段类型:大整数,和 users.id 类型一致
        ForeignKey("users.id"),     # 外键:关联 users 表的 id,必须是已存在的用户id
        nullable=True,              # 可空:未配负责人时为空
        index=True,                 # 建立索引,按 manager_id 查询更快
        comment="部门负责人(经理)ID"  # 字段备注
    )


    # ===== 基本信息 =====
    # name:部门名称,非空(如:研发部、财务部)
    name = Column(
        String(50),                 # 字段类型:字符串,长度50
        nullable=False,             # 非空:部门必须有名称
        comment="部门名称"           # 字段备注
    )

    # description:部门备注,可空
    description = Column(
        String(200),            # 字段类型:字符串,长度200
        nullable=True,          # 可空:备注可选
        comment="部门备注"       # 字段备注
    )


    # ===== 状态与时间 =====
    # is_active:是否启用,默认 True
    is_active = Column(
        Boolean,                  # 字段类型:布尔值
        default=True,             # 默认启用
        comment="是否启用"          # 字段备注
    )

    # created_at 记录创建时间 / updated_at 记录最后修改时间 已经在 base.py里写好


    # ===== 关联关系（ORM 导航，不产生新字段）=====
    # 该部门的负责人经理(指定 foreign_keys,避免与 User 侧关系歧义)
    manager = relationship(
        "User",                 # 关联目标模型
        foreign_keys=[manager_id],        # 指定外键
    )

