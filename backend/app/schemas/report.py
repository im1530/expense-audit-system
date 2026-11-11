"""
报表模块数据模型（管理端统计出参）
    四组只读出参，与 services.report_service 的返回结构一一对应：
        - ReportOverview：总览 + 各状态分布
        - ReportMonthlyItem：按月趋势
        - ReportDepartmentItem：按部门分布
        - ReportCategoryItem：按费用类别分布
    金额一律用 Decimal 序列化，前端按分/元自行格式化。
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel



# 状态分布项
class ReportStatusItem(BaseModel):
    """
    单据状态分布项
        :ivar status: 状态值，如 pending
        :ivar label: 状态中文名，如 待审批
        :ivar count: 该状态的单据数
        :ivar amount: 该状态的金额合计
    """
    status: str                 # 状态值
    label: str                  # 状态中文名
    count: int                  # 单据数
    amount: Decimal             # 金额合计



# 总览
class ReportOverview(BaseModel):
    """
    报表总览
        :ivar total_count: 单据总数（区间内、筛选后）
        :ivar total_amount: 金额合计
        :ivar pending_count: 待审批单据数
        :ivar pending_amount: 待审批金额
        :ivar status_items: 各状态分布（固定按订单状态顺序输出，含 0 的项）
    """
    total_count: int                        # 单据总数
    total_amount: Decimal                   # 金额合计
    pending_count: int                      # 待审批数
    pending_amount: Decimal                 # 待审批金额
    status_items: List[ReportStatusItem]    # 状态分布



# 按月趋势项
class ReportMonthlyItem(BaseModel):
    """
    按月统计项
        :ivar month: 月份，格式 YYYY-MM
        :ivar expense_count: 单据数
        :ivar total_amount: 总金额
        :ivar paid_amount: 已支付金额
    """
    month: str                  # 月份 YYYY-MM
    expense_count: int          # 单据数
    total_amount: Decimal       # 总金额
    paid_amount: Decimal        # 已支付金额



# 部门统计项
class ReportDepartmentItem(BaseModel):
    """
    部门统计项
        :ivar department_id: 部门ID；没挂部门的用户数据为 null
        :ivar department_name: 部门名称，未分配时显示"未分配"
        :ivar expense_count: 单据数
        :ivar total_amount: 总金额
        :ivar paid_amount: 已支付金额
        :ivar pending_count: 待审批单据数
    """
    department_id: Optional[int] = None     # 部门ID
    department_name: str                    # 部门名称
    expense_count: int                      # 单据数
    total_amount: Decimal                   # 总金额
    paid_amount: Decimal                    # 已支付金额
    pending_count: int                      # 待审批数



# 类别统计项
class ReportCategoryItem(BaseModel):
    """
    费用类别统计项（按小类聚合）
        :ivar category_id: 类别ID
        :ivar category_name: 类别名称
        :ivar expense_type: 所属费用大类值
        :ivar expense_type_label: 所属费用大类中文名
        :ivar item_count: 明细条数
        :ivar total_amount: 金额合计
    """
    category_id: int                        # 类别ID
    category_name: str                      # 类别名称
    expense_type: str                       # 大类值
    expense_type_label: str                 # 大类中文名
    item_count: int                         # 明细条数
    total_amount: Decimal                   # 金额合计
