"""
分页工具函数集（所有列表接口共用的分页基础设施）
    统一分页算法与响应结构，保证各列表接口的分页行为与边界处理完全一致：
        - calculate_total_pages：向上取整计算总页数，每页条数非法时返回 0
        - paginate_query：SQL 层分页（offset + limit），适合数据量大、能在数据库侧完成过滤的场景
        - paginate_list：内存分页（列表切片），适合必须逐条做权限判断后才能分页的场景（如审批待办）
        - assemble_paginated_result：把当前页数据与分页元信息组装成对应的响应模型实例
    选型建议：能下推到 SQL 的过滤优先用 paginate_query（避免把全量数据捞进内存）；
    只有在权限规则无法用 SQL 表达时，才退回"取全量 → 内存过滤 → paginate_list"的两段式做法。
"""


from typing import List, TypeVar, Generic
from sqlalchemy.orm import Query
from pydantic import BaseModel



# 计算总页数
def calculate_total_pages(total_items: int, page_size: int) -> int:
    """
    计算总页数（向上取整）
        实现要点：`(总数 + 每页数 - 1) // 每页数` 是整数除法下实现 ceil 的常用写法，
        例如 21 条 / 每页 10 条 = 3 页（而不是 2 页）。
        边界：每页条数 <= 0 时直接返回 0，避免除零异常；总数为 0 时得到 0 页。
        :param total_items: 符合条件的总记录数
        :param page_size: 每页条数
        :return: 总页数（整数，最小为 0）
    """
    if page_size <= 0:
        return 0
    return (total_items + page_size - 1) // page_size       # 总页数 = (总记录数 + 每页条数 - 1) // 每页条数



# SQLAlchemy查询对象分页
def paginate_query(query: Query, page: int, page_size: int) -> tuple[List, int]:
    """
    SQLAlchemy 查询对象分页（数据库侧完成 limit/offset）
        执行两条 SQL：一条 count() 取总数，一条 offset/limit 取当前页数据；
        调用前应先 order_by，否则分页结果顺序不稳定（同一条数据可能出现在不同页）。
        注意：多表 join 且存在一对多关系时，count() 可能大于实际去重后的条数，
        这种情况应先 distinct 或改用子查询再分页。
        :param query: 已完成权限过滤、筛选与排序的查询对象
        :param page: 当前页码，从 1 开始
        :param page_size: 每页条数
        :return: (当前页数据列表, 总记录数)，第二个值用于计算总页数
    """
    # 1.先查总记录数（用于计算总页数）
    total = query.count()                   # 总记录数
    # 2.按 offset/limit 取当前页数据
    items = (query
             .offset((page - 1) * page_size)  # 跳过前面n页的条数 (第1页 跳过0条; 第2页 跳过10条...)
             .limit(page_size)                # 本页最多取几条
             .all()                           # 执行并返回列表
        )
    # 3.返回 (当前页数据, 总记录数)
    return items, total



# 泛型声明:支持任意类型的列表项
T = TypeVar("T")                    # 泛型类型变量：支持任意列表项类型
# 内存列表分页
def paginate_list(data_list: List[T], page: int, page_size: int) -> tuple[List[T], int]:
    """
    内存列表分页（对已完成过滤的内存列表做切片）
        适用场景：权限规则依赖逐条判断（如按审批层级角色匹配待办），无法在 SQL 里表达时，
        只能先取回候选集、在内存中过滤，再用本函数切页，保证"总条数"与"当前页数据"完全一致。
        代价：一次会加载全部候选数据，仅适合候选集规模可控的场景。
        :param data_list: 已过滤、已排序完成的数据列表
        :param page: 当前页码，从 1 开始
        :param page_size: 每页条数
        :return: (当前页数据列表, 总记录数)；页码越界时返回空列表，但总数保持真实值
    """
    # 1.总记录数就是内存列表长度
    total = len(data_list)              # 总记录数
    # 2.计算当前页的起止下标
    start = (page - 1) * page_size      # 计算当前页的起始下标
    end = start + page_size             # 计算当前页的结束下标
    # 3.切片取出当前页（页码越界时自然得到空列表）
    items = data_list[start:end]        # 切片:获取当前页数据
    # 4.返回 (当前页数据, 总记录数)
    return items, total



# 泛型声明:支持任意类型的响应模型
ResponseModelType = TypeVar("ResponseModelType", bound=BaseModel)    # 泛型类型变量：必须继承 BaseModel
# 分页结果组装
def assemble_paginated_result(
        items: List,                            # 当前页数据（ORM 对象或字典）
        total: int,                             # 符合条件的总条数
        page: int,                              # 当前页码
        page_size: int,                         # 每页条数
        response_model: type[ResponseModelType]  # 分页响应模型类
) -> ResponseModelType:
    """
    分页结果组装：把当前页数据与分页元信息组装成统一的分页响应模型实例
        各列表接口的返回结构一致（items / total / page / page_size / total_pages），前端可用同一套分页组件。
        items 里既可以是 ORM 对象，也可以是手工组装的字典；响应模型的 from_attributes 负责完成字段映射。
        :param items: 当前页数据列表（ORM 对象或字典均可）
        :param total: 符合条件的总记录数（不是本页条数）
        :param page: 当前页码，从 1 开始
        :param page_size: 每页条数
        :param response_model: 分页响应模型类，如 ExpenseListResponse
        :return: 实例化后的分页响应模型对象，可直接作为接口返回值
    """
    total_pages = calculate_total_pages(total, page_size)       # 计算总页数
    return response_model(                                     # 实例化响应模型
        items=items,                # 当前页数据列表
        total=total,                # 总记录数
        page=page,                  # 当前页码
        page_size=page_size,        # 每页条数
        total_pages=total_pages,    # 总页数
    )


