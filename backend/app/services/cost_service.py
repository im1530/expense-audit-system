"""
成本统计与预算控制（Phase F）
    职责：
        1. 按 token 数估算费用（单价来自配置，0 表示未配置）；
        2. 统计当日预审调用次数、token 与费用；
        3. 判断是否超过每日预算；
        4. 统计凭证识别当日调用次数并判断限额。
    口径：
        - 预审调用次数只统计非兜底完成记录（与原有 AI_REVIEW_DAILY_LIMIT 一致）；
        - token/费用统计所有实际产生 token 的记录（含兜底前的失败调用）；
        - 凭证识别按“当天创建的识别记录”计数，失败尝试也计入，防止无限重试绕过限额。
"""
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIReviewLog, AIReviewStatus, DocumentExtraction



# token 单价换算基数：价格按“百万 token”配置
_PRICE_BASE = Decimal("1000000")

# 费用精度：保留 6 位小数，避免浮点误差
_COST_QUANT = Decimal("0.000001")



# 计算费用
def estimate_cost(prompt_tokens: int | None, completion_tokens: int | None) -> Decimal:
    """
    按配置单价估算一次调用的费用
        :param prompt_tokens: 输入 token 数
        :param completion_tokens: 输出 token 数
        :return: 费用（元，6 位小数）
    """
    prompt = Decimal(int(prompt_tokens or 0))                           # 输入 token
    completion = Decimal(int(completion_tokens or 0))                   # 输出 token
    input_price = Decimal(str(settings.MODEL_INPUT_PRICE_PER_1M or 0))  # 输入单价
    output_price = Decimal(str(settings.MODEL_OUTPUT_PRICE_PER_1M or 0))# 输出单价
    cost = (prompt / _PRICE_BASE) * input_price + (completion / _PRICE_BASE) * output_price
    return cost.quantize(_COST_QUANT)



# 当天起点
def today_start() -> datetime:
    """
    今天的 00:00:00
        :return: 当天起点
    """
    return datetime.combine(date.today(), time.min)



# 统计当日预审用量
def daily_review_usage(db: Session, *, exclude_id: int | None = None) -> dict:
    """
    统计当天预审调用次数、token 与费用
        :param db: 数据库会话
        :param exclude_id: 不统计当前任务自身
        :return: {"calls": int, "tokens": int, "prompt_tokens": int, "completion_tokens": int, "cost": Decimal}
    """
    start = today_start()                                               # 当天起点

    # 1.调用次数：与原有口径一致，只算非兜底的完成记录
    call_query = db.query(func.count(AIReviewLog.id)).filter(
        AIReviewLog.created_at >= start,
        AIReviewLog.status.in_([
            AIReviewStatus.SUCCESS,
            AIReviewStatus.FALLBACK,
            AIReviewStatus.FAILED,
        ]),
        AIReviewLog.is_fallback == False,
    )
    if exclude_id is not None:
        call_query = call_query.filter(AIReviewLog.id != exclude_id)
    calls = int(call_query.scalar() or 0)

    # 2.token 统计：包含成功、降级、失败（失败前可能已经产生 token）
    token_query = db.query(
        func.coalesce(func.sum(func.coalesce(AIReviewLog.prompt_tokens, 0)), 0),
        func.coalesce(func.sum(func.coalesce(AIReviewLog.completion_tokens, 0)), 0),
    ).filter(
        AIReviewLog.created_at >= start,
        AIReviewLog.status.in_([
            AIReviewStatus.SUCCESS,
            AIReviewStatus.FALLBACK,
            AIReviewStatus.FAILED,
        ]),
    )
    if exclude_id is not None:
        token_query = token_query.filter(AIReviewLog.id != exclude_id)
    prompt_tokens, completion_tokens = token_query.one()
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)

    # 3.凭证识别也消耗 token：并入当日总量，避免只统计风险评审而低估成本
    doc_prompt, doc_completion = (
        db.query(
            func.coalesce(func.sum(func.coalesce(DocumentExtraction.prompt_tokens, 0)), 0),
            func.coalesce(func.sum(func.coalesce(DocumentExtraction.completion_tokens, 0)), 0),
        )
        .filter(DocumentExtraction.created_at >= start)
        .one()
    )
    prompt_tokens += int(doc_prompt or 0)
    completion_tokens += int(doc_completion or 0)

    # 4.费用按当前单价估算
    return {
        "calls": calls,
        "tokens": prompt_tokens + completion_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost": estimate_cost(prompt_tokens, completion_tokens),
    }



# 预审预算判断
def review_budget_reason(db: Session, *, exclude_id: int | None = None) -> str | None:
    """
    判断是否超过每日预审预算
        :param db: 数据库会话
        :param exclude_id: 不统计当前任务自身
        :return: 超限原因；未超限返回 None
    """
    usage = daily_review_usage(db, exclude_id=exclude_id)               # 当日用量

    # 1.调用次数上限（沿用原有配置）
    if settings.AI_REVIEW_DAILY_LIMIT > 0 and usage["calls"] >= settings.AI_REVIEW_DAILY_LIMIT:
        return "daily_limit"

    # 2.token 预算
    if settings.AI_REVIEW_DAILY_TOKEN_LIMIT > 0 and usage["tokens"] >= settings.AI_REVIEW_DAILY_TOKEN_LIMIT:
        return "daily_token_limit"

    # 3.费用预算
    if settings.AI_REVIEW_DAILY_COST_LIMIT > 0 and usage["cost"] >= Decimal(str(settings.AI_REVIEW_DAILY_COST_LIMIT)):
        return "daily_cost_limit"

    return None



# 凭证识别当日调用数
def document_calls_today(db: Session) -> int:
    """
    统计当天创建的凭证识别记录数
        :param db: 数据库会话
        :return: 记录数
    """
    return int(
        db.query(func.count(DocumentExtraction.id))
        .filter(DocumentExtraction.created_at >= today_start())
        .scalar()
        or 0
    )



# 凭证识别每日限额判断
def document_daily_limit_reached(db: Session) -> bool:
    """
    判断凭证识别是否达到每日调用上限
        :param db: 数据库会话
        :return: 超限 True
    """
    limit = int(settings.DOCUMENT_DAILY_LIMIT or 0)
    if limit <= 0:
        return False
    return document_calls_today(db) >= limit
