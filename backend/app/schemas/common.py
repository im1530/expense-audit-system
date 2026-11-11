"""
通用公共数据模型（所有列表接口共享的入参）
    提供全系统统一的分页与筛选参数，避免每个接口各写一套、前后端约定不一致：
        - PageParams：分页参数，统一定义页码与每页条数的校验规则，通过 Depends() 注入到所有分页列表接口
        - BusinessFilter：通用业务筛选参数，封装费用大类、费用小类、日期区间、金额区间，
          报销单列表与审批列表共用；状态、指定员工等差异化参数由各接口自行补充
    说明：两者都是"纯入参模型"，会被 FastAPI 展开成 Query 参数，不出现在任何响应体中。
    注意：同一组筛选参数在不同列表接口含义略有差异——报销单列表按"单据创建时间"过滤日期、
    按"单据总额"过滤金额；明细列表按"费用发生日期"和"单条明细金额"过滤，
    具体实现见 services.expense_service 中的两个 apply_*_filters 函数。
"""



from pydantic import BaseModel, Field
from decimal import Decimal
from typing import Optional
from datetime import date
from app.models.expense import ExpenseType



# 通用分页参数模型
class PageParams(BaseModel):
    """
    通用分页参数模型（所有分页列表接口统一使用，保证分页规则与文档描述一致）
        通过 `page_params: PageParams = Depends()` 注入，FastAPI 会自动解析同名 Query 参数并做校验，
        校验不通过时直接返回 422，无需在接口里手写判断。
        :ivar page: 页码，从第 1 页开始，小于 1 视为非法
        :ivar page_size: 每页条数，范围 2~100（上限防止一次拉全表拖垮数据库）
    """
    page: int = Field(1, ge=1, description="页码,从第1页开始")          # 第几页，最小 1
    page_size: int = Field(10, gt=1, le=100, description="每页条数,最大100条")  # 每页条数，2~100



# 通用业务筛选参数模型
class BusinessFilter(BaseModel):
    """
    通用业务筛选参数模型（所有报销单列表、审批列表、明细列表 100% 通用的筛选条件）
        全部字段可选，不传即视为不过滤，最终由 services 层的 apply_business_filters /
        apply_item_business_filters 翻译成 SQL 条件；各接口的差异化参数（状态、指定员工、指定部门）另行声明。
        :ivar expense_type: 明细所属费用大类，命中"存在该大类明细"的单据
        :ivar category_id: 明细所属费用小类ID，命中"存在该小类明细"的单据
        :ivar start_date: 日期区间起点；单据维度指创建时间，明细维度指费用发生日期
        :ivar end_date: 日期区间终点，同上
        :ivar min_amount: 金额下限；单据维度指报销总金额，明细维度指单条明细金额
        :ivar max_amount: 金额上限，同上
    """
    expense_type: Optional[ExpenseType] = Field(None, description="筛选:明细所属费用大类")  # 费用大类筛选（可选）
    category_id: Optional[int] = Field(None, description="筛选:明细所属费用分类ID(小类)")    # 费用小类筛选（可选）
    start_date: Optional[date] = Field(None, description="筛选:日期(起始)")                # 日期区间起点（可选）
    end_date: Optional[date] = Field(None, description="筛选:日期(结束)")                  # 日期区间终点（可选）
    min_amount: Optional[Decimal] = Field(None, ge=0, description="筛选:报销总金额下限")    # 金额下限（可选）
    max_amount: Optional[Decimal] = Field(None, ge=0, description="筛选:报销总金额上限")    # 金额上限（可选）

