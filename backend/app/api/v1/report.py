"""
报表路由（挂载前缀 /api/v1/report）
    给财务 / 老板 / 管理员使用的经营视角统计，全部只读：
        - GET "/overview"   总览（单据数、金额、各状态分布）
        - GET "/monthly"    按月趋势
        - GET "/department" 按部门分布
        - GET "/category"   按费用类别分布
        - GET "/export"     导出明细清单 Excel（xlsx）
    权限：
        - 统计接口要求 view_all（财务 / 老板 / 管理员）；
        - 导出接口额外要求 export（当前只有财务与管理员有，老板默认没有导出权限）。
    口径：
        - 日期区间基于报销单创建时间，结束日按"含当天"处理；
        - 部门维度按提交人当前部门统计；没部门的用户归为"未分配"。
"""
from datetime import date, datetime
from io import BytesIO
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user               # 当前登录用户依赖
from app.models import User                             # 用户模型
from app.models.expense import ExpenseStatus            # 单据状态枚举
from app.schemas.report import (                        # 出参模型
    ReportCategoryItem,
    ReportDepartmentItem,
    ReportMonthlyItem,
    ReportOverview,
)
from app.services.excel_service import build_xlsx       # 标准库生成 xlsx
from app.services.report_service import (               # 统计逻辑
    get_category_report,
    get_department_report,
    get_export_rows,
    get_monthly_trend,
    get_overview,
)



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 报表查看权限校验
def _require_view_all(current_user: User) -> None:
    """
    校验当前用户是否有"查看全公司数据"的权限（统计接口统一入口）
        :param current_user: 当前登录用户
        :raises HTTPException: 403 无权限
    """
    if not current_user.has_permission("view_all"):
        raise HTTPException(status_code=403, detail="无权限查看公司报表")


# 导出权限校验
def _require_export(current_user: User) -> None:
    """
    校验当前用户是否有导出权限
        :param current_user: 当前登录用户
        :raises HTTPException: 403 无权限
    """
    if not current_user.has_permission("export"):
        raise HTTPException(status_code=403, detail="无权限导出报表")



# 总览统计
@router.get(
    "/overview",                    # 接口路径；拼上总前缀后完整为 /api/v1/report/overview
    response_model=ReportOverview,  # 返回总览结构
    tags=["报表"],                   # 在 /api/docs 文档里归到"报表"分组显示
    summary="报表总览(单据数/金额/状态分布)",  # 接口标题
)
def report_overview(
    start_date: Optional[date] = Query(None, description="统计起始日期(含)"),  # 统计起始日
    end_date: Optional[date] = Query(None, description="统计结束日期(含)"),  # 统计结束日
    department_id: Optional[int] = Query(None, ge=1, description="按部门筛选(提交人所属部门)"),  # 部门筛选
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    报表总览：单据数、金额、各状态分布、待审批情况
    用途: 财务/老板打开报表页的第一屏卡片
        :param start_date: 可选，统计起始日期（含当天）
        :param end_date: 可选，统计结束日期（含当天）
        :param department_id: 可选，按部门筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户（需 view_all）
        :return: ReportOverview
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验：报表统计要求 view_all
    _require_view_all(current_user)
    # 2.调用服务层统计并返回
    return get_overview(db, start_date=start_date, end_date=end_date, department_id=department_id)



# 按月趋势
@router.get(
    "/monthly",                         # 接口路径；拼上总前缀后完整为 /api/v1/report/monthly
    response_model=List[ReportMonthlyItem],  # 返回按月列表
    tags=["报表"],                       # 在 /api/docs 文档里归到"报表"分组显示
    summary="按月统计报销趋势",             # 接口标题
)
def report_monthly(
    start_date: Optional[date] = Query(None, description="统计起始日期(含)"),  # 统计起始日
    end_date: Optional[date] = Query(None, description="统计结束日期(含)"),  # 统计结束日
    department_id: Optional[int] = Query(None, ge=1, description="按部门筛选(提交人所属部门)"),  # 部门筛选
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    按月份统计单据数、总金额、已支付金额（MySQL date_format 按月截断）
        :param start_date: 可选，统计起始日期（含当天）
        :param end_date: 可选，统计结束日期（含当天）
        :param department_id: 可选，按部门筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户（需 view_all）
        :return: 按月列表，按月份升序
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验：报表统计要求 view_all
    _require_view_all(current_user)
    # 2.调用服务层统计并返回
    return get_monthly_trend(db, start_date=start_date, end_date=end_date, department_id=department_id)



# 部门分布
@router.get(
    "/department",                          # 接口路径；拼上总前缀后完整为 /api/v1/report/department
    response_model=List[ReportDepartmentItem],   # 返回部门列表
    tags=["报表"],                            # 在 /api/docs 文档里归到"报表"分组显示
    summary="按部门统计报销分布",                # 接口标题
)
def report_department(
    start_date: Optional[date] = Query(None, description="统计起始日期(含)"),  # 统计起始日
    end_date: Optional[date] = Query(None, description="统计结束日期(含)"),  # 统计结束日
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    按部门统计单据数、总金额、已支付金额、待审批数量
        没挂部门的用户（如 boss）归为"未分配"一组，避免数据凭空消失。
        :param start_date: 可选，统计起始日期（含当天）
        :param end_date: 可选，统计结束日期（含当天）
        :param db: 数据库会话
        :param current_user: 当前登录用户（需 view_all）
        :return: 部门列表，按金额倒序
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验：报表统计要求 view_all
    _require_view_all(current_user)
    # 2.调用服务层统计并返回
    return get_department_report(db, start_date=start_date, end_date=end_date)



# 类别分布
@router.get(
    "/category",                            # 接口路径；拼上总前缀后完整为 /api/v1/report/category
    response_model=List[ReportCategoryItem],    # 返回类别列表
    tags=["报表"],                            # 在 /api/docs 文档里归到"报表"分组显示
    summary="按费用类别统计报销分布",              # 接口标题
)
def report_category(
    start_date: Optional[date] = Query(None, description="统计起始日期(含)"),  # 统计起始日
    end_date: Optional[date] = Query(None, description="统计结束日期(含)"),  # 统计结束日
    department_id: Optional[int] = Query(None, ge=1, description="按部门筛选(提交人所属部门)"),  # 部门筛选
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    按费用类别（小类）统计明细条数与金额
        :param start_date: 可选，统计起始日期（含当天）
        :param end_date: 可选，统计结束日期（含当天）
        :param department_id: 可选，按部门筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户（需 view_all）
        :return: 类别列表，按金额倒序
        :raises HTTPException: 403 无权限
    """
    # 1.权限校验：报表统计要求 view_all
    _require_view_all(current_user)
    # 2.调用服务层统计并返回
    return get_category_report(db, start_date=start_date, end_date=end_date, department_id=department_id)



# 导出明细清单
@router.get(
    "/export",                      # 接口路径；拼上总前缀后完整为 /api/v1/report/export
    tags=["报表"],                   # 在 /api/docs 文档里归到"报表"分组显示
    summary="导出报销明细清单(xlsx)",    # 接口标题
)
def report_export(
    start_date: Optional[date] = Query(None, description="导出起始日期(含)"),  # 导出起始日
    end_date: Optional[date] = Query(None, description="导出结束日期(含)"),  # 导出结束日
    department_id: Optional[int] = Query(None, ge=1, description="按部门筛选(提交人所属部门)"),  # 部门筛选
    status: Optional[ExpenseStatus] = Query(None, description="按单据状态筛选"),  # 单据状态
    db: Session = Depends(get_db),                                  # 注入数据库会话
    current_user: User = Depends(get_current_user),                 # 当前登录人（验签）
):
    """
    导出报销明细清单（一行 = 一条明细）为 xlsx 文件
    实现说明:
        - 用标准库 zipfile 手写 OOXML，不依赖 openpyxl；
        - 最多导出 20000 行，防止误操作把内存打爆；
        - 文件名走 RFC 5987 编码，中文名在浏览器里不会乱码。
    权限: 需要 export 权限（财务 / 管理员），老板默认没有。
        :param start_date: 可选，起始日期（含当天）
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按部门筛选
        :param status: 可选，按单据状态筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户（需 export）
        :return: xlsx 文件流
        :raises HTTPException: 403 无权限
    """
    _require_export(current_user)

    # 1.取数据（已转成适合写 Excel 的形式）
    rows = get_export_rows(  # 导出数据行
        db,                                     # 数据库会话
        start_date=start_date,                  # 导出起始日期（含当天）
        end_date=end_date,                      # 导出结束日期（含当天）
        department_id=department_id,            # 按提交人所属部门筛选
        status=status,                          # 按单据状态筛选
    )

    # 2.表头与列宽（与 report_service.get_export_rows 的列顺序严格对应）
    header = [  # 表头列名
        "报销单号", "标题", "提交人", "部门", "单据状态", "单据金额",  # 表头列名
        "费用说明", "费用类别", "费用大类", "明细金额", "发生日期", "发票号", "创建时间",  # 表头列名
    ]
    widths = [24, 26, 10, 12, 10, 12, 30, 14, 10, 12, 12, 18, 20]  # 各列宽度

    # 3.生成 xlsx
    content = build_xlsx("报销明细", header, rows, widths)  # xlsx 字节内容

    # 4.文件名：中文走 filename*，同时给一个 ASCII 兜底名
    filename = f"报销明细_{datetime.now():%Y%m%d_%H%M%S}.xlsx"  # 导出文件名
    disposition = f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(filename)}"  # 下载响应头

    # 5.以文件流返回（浏览器直接下载，不需要先落盘）
    return StreamingResponse(
        BytesIO(content),  # 文件内容
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # 文件类型
        headers={"Content-Disposition": disposition},  # 下载响应头
    )
