"""
数据库基础模型（所有 ORM 模型的父级约定）
    提供全表通用的能力，避免每张表重复定义：
        - 表名规则：CustomBase.__tablename__ 声明"类名小写 + s"的自动表名兜底逻辑
        - 通用时间字段：created_at（插入时由数据库自动填）、updated_at（每次 UPDATE 自动刷新）
    对外只暴露 Base：所有模型统一继承 Base，例如 `class User(Base)`，这样同一套元数据（metadata）里才能
    正确建立外键与 relationship 的映射。
    注意：本项目所有模型都显式写了 __tablename__，自动表名仅作为约定与兜底说明保留。
"""


from sqlalchemy import Column, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import declared_attr




class CustomBase:
    """
    自定义基类（不直接映射成表，只提供通用规则与字段）
        被 declarative_base(cls=CustomBase) 混入到 Base 后，所有模型都会自动获得：
            - __tablename__ 的自动命名约定（类名小写 + s）
            - created_at / updated_at 两个通用时间字段
        子类可以覆盖任意一项，例如显式写 __tablename__ = "users" 覆盖默认表名规则。
    """

    # 自动生成表名约束：类名 User → 表名 users（类名小写 + 补 s）
    @declared_attr      # declared_attr：把这个属性延迟到子类定义时再生效
    def __tablename__(cls):
        """
        自动表名兜底规则：把类名转成小写并补 "s" 作为表名
            作用：给忘记写 __tablename__ 的模型一个可用的默认表名。
            注意：本项目所有模型都已显式声明表名，实际不会被触发；
            若要启用该兜底，取值应基于类名（如 cls.__name__），不能用 cls.__tablename__ 自引用。
            :return: 小写化的表名字符串
        """
        return cls.__tablename__.lower() + "s"

    # ===== 通用时间字段 =====
    # created_at：记录创建时间
    created_at = Column(
        DateTime(timezone=True),            # 类型：日期时间，timezone=True 带时区
        server_default=func.now(),          #【数据库端默认值】INSERT 时由数据库自动填当前时间，代码不用传
        nullable=False,                     # 不允许为 NULL（创建时间必须有）
        comment="创建时间"                    # 字段备注
    )
    # updated_at：记录最后修改时间
    updated_at = Column(
        DateTime(timezone=True),            # 类型：日期时间，timezone=True 带时区
        onupdate=func.now(),                # 【更新时触发】每次执行 UPDATE，数据库自动刷新为当前时间
        comment="更新时间"                    # 字段备注
    )


# 把 CustomBase 挂到 SQLAlchemy 的声明基类，以后模型继承的就是它
Base = declarative_base(cls=CustomBase)     # 所有 ORM 模型的声明基类