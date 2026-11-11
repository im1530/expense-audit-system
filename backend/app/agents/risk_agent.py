"""
风险评审 Agent（Phase B：规则 + 制度检索 + LLM 单 Agent）
    处理链路：
        context（已脱敏/截断） + policy（检索到的制度条款）
        → Prompt → LLM → Schema 校验 → 业务归一化 → 引用校验 → ReviewOutcome
    企业级约束：
        1. 模型输出一律视为不可信：先过 Pydantic，再做分数夹取与等级重算；
        2. 规则优先：rule_hits 永远并入 findings，block/review 会抬高最终等级；
        3. 制度引用严格校验：只允许引用本轮注入的条款，且必须是原文连续片段；
        4. 无制度上下文时引用一律丢弃，防止模型编造"依据第几条"；
        5. 本模块不写数据库：持久化、幂等、并发、降级由 service 层负责。
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from pydantic import ValidationError

from app.agents.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_policy_context,
    build_user_prompt,
)
from app.config import settings
from app.rag.citation import validate_citations
from app.rag.schema import PolicyBundle
from app.schemas.ai_review import AIReviewOutput
from app.services.llm_client import (
    LLMClient,
    LLMResponseError,
    LLMResult,
    build_llm_client,
)



logger = logging.getLogger(__name__)

# 等级顺序：用于取较高等级
LEVEL_ORDER = {"low": 0, "mid": 1, "high": 2}

# 规则级别 → AI 级别
RULE_LEVEL_TO_AI = {"warn": "low", "review": "mid", "block": "high"}

# 规则类型 → 用于识别"AI 是否在重复描述规则命中"的关键词
RULE_TYPE_KEYWORDS = {
    "check_category_limit": ("限额", "超限", "超出", "超过"),
    "check_invoice_missing": ("发票", "无票", "票据"),
    "check_duplicate": ("重复",),
    "check_period_late": ("跨期", "超期", "时效", "逾期"),
}

# 单次输出最多保留的风险点
MAX_FINDINGS = 20



# Agent 的最终输出（已归一化，可直接落库）
@dataclass(frozen=True)
class ReviewOutcome:
    """
    一次预审的最终结果
        :ivar risk_score: 0~100 的分数（两位小数）
        :ivar risk_level: low/mid/high（按分数重算，并受规则下限约束）
        :ivar summary: 总结
        :ivar findings: 风险点列表（规则 + AI，已去重/排序/截断）
        :ivar policy_citations: 通过校验的制度引用
        :ivar is_fallback: 是否规则兜底
        :ivar model: 模型名（兜底时为 rules）
        :ivar prompt_version: Prompt 版本
        :ivar latency_ms: 调用耗时
        :ivar prompt_tokens: 输入 token
        :ivar completion_tokens: 输出 token
        :ivar raw_content: 模型原始文本（排障用）
        :ivar policy_version: 本次使用的制度版本
        :ivar retrieved_clause_ids: 本轮注入 Prompt 的条款编号
        :ivar citation_stats: 引用校验审计统计
    """
    risk_score: Decimal                 # 风险分数
    risk_level: str                     # 风险等级
    summary: str                        # 总结
    findings: list[dict]                # 风险点
    policy_citations: list[dict]        # 制度引用
    is_fallback: bool                   # 是否兜底
    model: str                          # 模型名
    prompt_version: str                 # Prompt 版本
    latency_ms: int                     # 耗时
    prompt_tokens: int                  # 输入 token
    completion_tokens: int              # 输出 token
    raw_content: str = ""               # 原始文本
    policy_version: str = ""            # 制度版本
    retrieved_clause_ids: list[str] = field(default_factory=list)   # 注入的条款编号
    citation_stats: dict = field(default_factory=dict)              # 引用审计统计



# 按分数计算等级（分数是唯一事实来源）
def level_for_score(score: Decimal) -> str:
    """
    按阈值把分数转成等级
        :param score: 0~100 的分数
        :return: low / mid / high
    """
    if score >= Decimal("70"):
        return "high"
    if score >= Decimal("40"):
        return "mid"
    return "low"



# 取两个等级中较高的一个
def higher_level(first: str, second: str) -> str:
    """
    返回较高的风险等级
        :param first: 等级一
        :param second: 等级二
        :return: 较高的等级
    """
    return first if LEVEL_ORDER.get(first, 0) >= LEVEL_ORDER.get(second, 0) else second



# 把规则命中共换成 findings
def build_rule_findings(context: dict) -> list[dict]:
    """
    把上下文里的 rule_hits 转成统一的风险点
        :param context: build_review_context 的结果
        :return: findings 列表
    """
    findings = []
    for hit in context.get("rule_hits", []):
        level = RULE_LEVEL_TO_AI.get(hit.get("level"), "low")          # 规则级别映射
        message = str(hit.get("message") or "规则命中")[:500]           # 提示文案
        findings.append({
            "type": "rule",                                            # 类型：规则
            "rule_type": hit.get("rule_type"),                          # 规则类型（用于识别 AI 重复）
            "level": level,                                            # 级别
            "message": message,                                        # 说明
            "evidence": hit.get("rule_label") or hit.get("rule_type"),  # 证据：规则标签
            "suggestion": "必须人工核对" if level == "high" else None,   # 高风险必须人工处理
        })
    return findings



# 把凭证校验结论转成 findings
def build_document_findings(context: dict) -> list[dict]:
    """
    把上下文里的 document_findings 转成统一的风险点
        这些结论由 document_service 基于票面事实确定性生成，
        不交给模型自由改写。
        :param context: 预审上下文
        :return: findings 列表
    """
    findings: list[dict] = []                                       # 结果
    for item in context.get("document_findings", []) or []:
        message = str(item.get("message") or "").strip()             # 提示
        if not message:
            continue                                                # 空提示跳过
        level = str(item.get("level") or "low")                      # 级别
        findings.append({
            "type": "invoice",                                      # 凭证类风险
            "level": level if level in LEVEL_ORDER else "low",      # 级别兜底
            "message": message[:500],                               # 说明
            "evidence": str(item.get("evidence") or "")[:500] or None,   # 证据
            "suggestion": str(item.get("suggestion") or "")[:300] or None,  # 建议
            "clause_id": None,                                      # 凭证风险不引用制度
        })
    return findings
def _find_duplicated_rule(ai_finding: dict, rule_findings: list[dict]) -> dict | None:
    """
    找到 AI 风险点对应的规则命中
        匹配策略：
            - type=invoice → 对应 check_invoice_missing；
            - type=duplicate → 对应 check_duplicate；
            - type=rule → 按规则类型关键词匹配（限额/发票/重复/跨期）。
        匹配到就视为重复，保留规则那条，不重复展示。
        :param ai_finding: 模型返回的风险点
        :param rule_findings: 规则命中的风险点
        :return: 命中的规则风险点；不是重复则 None
    """
    ai_type = ai_finding.get("type")
    message = str(ai_finding.get("message") or "")

    for rule in rule_findings:
        rule_type = rule.get("rule_type")
        if ai_type == "invoice" and rule_type == "check_invoice_missing":
            return rule
        if ai_type == "duplicate" and rule_type == "check_duplicate":
            return rule
        if ai_type == "rule":
            keywords = RULE_TYPE_KEYWORDS.get(rule_type, ())
            if any(keyword in message for keyword in keywords):
                return rule
    return None



# 合并规则与 AI 的风险点：规则优先、AI 只补充新风险
def merge_findings(rule_findings: list[dict], ai_findings: list[dict]) -> list[dict]:
    """
    合并两类风险点（方案 B：规则等级以配置为准）
        规则：
            1. 规则命中全部保留，等级/文案不被 AI 改写；
            2. AI 重复描述某条规则命中时丢弃该 AI 项，仅把 AI 建议补到规则项上；
            3. AI 只保留规则覆盖不到的新风险；
            4. 去重后按级别从高到低排序，最多 20 条。
        :param rule_findings: 规则命中的 findings
        :param ai_findings: 模型返回的 findings
        :return: 合并后的 findings（最多 20 条）
    """
    merged: list[dict] = []
    seen = set()

    # 1.先放入规则命中（拷贝一份，避免修改上下文里的原始字典）
    for item in rule_findings:
        message = str(item.get("message") or "").strip()
        key = (item.get("type"), message[:80].lower())
        if not message or key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))

    # 2.再处理 AI 风险点：重复规则的丢弃，新风险保留
    for item in ai_findings:
        message = str(item.get("message") or "").strip()
        if not message:
            continue

        duplicated = _find_duplicated_rule(item, merged)
        if duplicated is not None:
            # 规则等级/文案保持配置；AI 的建议如果更有用，补到规则项上
            if not duplicated.get("suggestion") and item.get("suggestion"):
                duplicated["suggestion"] = item["suggestion"]
            continue

        key = (item.get("type"), message[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    # 3.按级别从高到低排序
    merged.sort(key=lambda item: LEVEL_ORDER.get(item.get("level"), 0), reverse=True)
    return merged[:MAX_FINDINGS]



# 按规则命中设置分数/等级下限
def apply_rule_floor(
    *,
    score: Decimal,
    level: str,
    summary: str,
    rule_hits: list[dict],
) -> tuple[Decimal, str, str]:
    """
    用规则命中的确定性结论约束模型输出
        - block：分数至少 70、等级 high，并在总结前加"规则阻断"提示；
        - review：分数至少 40、等级至少 mid；
        - warn：不强制抬高等级。
        :param score: 模型分数
        :param level: 按分数算出的等级
        :param summary: 模型总结
        :param rule_hits: 上下文中的规则命中
        :return: (修正后的分数, 修正后的等级, 修正后的总结)
    """
    levels = {hit.get("level") for hit in rule_hits}

    if "block" in levels:
        score = max(score, Decimal("70"))
        level = "high"
        summary = "【规则阻断】存在阻止提交的规则命中，AI 结论不得作为通过依据。" + summary
    elif "review" in levels:
        score = max(score, Decimal("40"))
        level = higher_level(level, "mid")

    return score, level, summary[:1000]



# 按凭证校验结论设置分数/等级下限
def apply_document_floor(
    *,
    score: Decimal,
    level: str,
    summary: str,
    document_findings: list[dict],
) -> tuple[Decimal, str, str]:
    """
    用凭证事实的确定性结论约束模型输出
        - high：分数至少 70、等级 high；
        - mid：分数至少 40、等级至少 mid；
        - low：不强制抬高等级。
        :param score: 模型分数
        :param level: 按分数算出的等级
        :param summary: 模型总结
        :param document_findings: 凭证风险点
        :return: (修正后的分数, 修正后的等级, 修正后的总结)
    """
    levels = {str(item.get("level") or "low") for item in document_findings}   # 凭证级别集合

    if "high" in levels:
        score = max(score, Decimal("70"))
        level = "high"
        summary = "【凭证校验】存在高风险的票面/明细不一致，AI 结论不得作为通过依据。" + summary
    elif "mid" in levels:
        score = max(score, Decimal("40"))
        level = higher_level(level, "mid")

    return score, level, summary[:1000]



class RiskAgent:
    """
    风险评审 Agent
        依赖注入 LLMClient，便于测试时替换成 Fake；
        review() 只返回 ReviewOutcome，不写库。
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        *,
        system_prompt: str | None = None,
        prompt_version: str | None = None,
    ):
        """
        :param llm_client: LLM 客户端；不传则按配置创建
        :param system_prompt: 可选的 System Prompt（A/B 评测用）
        :param prompt_version: 可选的 Prompt 版本号（写入审计与结果）
        """
        self.llm = llm_client or build_llm_client()      # LLM 客户端
        self.system_prompt = system_prompt or SYSTEM_PROMPT   # 默认使用 v3
        self.prompt_version = prompt_version or PROMPT_VERSION  # 默认版本号

    # 执行一次预审
    async def review(self, context: dict, *, policy: PolicyBundle | None = None) -> ReviewOutcome:
        """
        对已构造好的上下文执行预审
            :param context: build_review_context 的结果
            :param policy: 本轮检索到的制度条款；None 表示无制度上下文
            :return: ReviewOutcome
            :raises LLMError: 调用失败（由上层决定是否降级）
            :raises LLMResponseError: 模型输出不符合 Schema
        """
        # 1.按字符预算裁剪条款：Prompt 与引用校验必须使用同一批条款
        effective_policy = (
            policy.limit_chars(settings.POLICY_MAX_CONTEXT_CHARS)
            if policy is not None
            else None
        )

        # 2.构造制度数据区（无制度时也会给出显式空区）
        policy_context = build_policy_context(effective_policy)

        # 3.调用 LLM（System Prompt 固定，数据区由 context 与制度条款生成）
        result = await self.llm.chat_json(
            system_prompt=self.system_prompt,
            user_prompt=build_user_prompt(context, policy_context),
        )

        # 4.结构校验：模型输出不可信，先过 Pydantic
        try:
            output = AIReviewOutput.model_validate(result.data)
        except ValidationError as exc:
            raise LLMResponseError(f"AI 输出不符合结构: {exc.errors()[:1]}", retryable=False) from exc

        # 5.业务归一化
        return self._normalize(output, context, result, effective_policy)

    # 模型输出 → 最终结果
    def _normalize(
        self,
        output: AIReviewOutput,
        context: dict,
        result: LLMResult,
        policy: PolicyBundle | None,
    ) -> ReviewOutcome:
        """
        把校验后的模型输出归一化成 ReviewOutcome
            :param output: 通过 Schema 的模型输出
            :param context: 预审上下文
            :param result: LLM 原始结果（耗时/token/文本）
            :param policy: 本轮实际注入的制度条款
            :return: 归一化结果
        """
        # 1.分数夹取到 0~100，并保留两位小数
        score = Decimal(str(output.risk_score)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        score = min(max(score, Decimal("0")), Decimal("100"))

        # 2.等级以分数为准，不直接信任模型自评
        level = level_for_score(score)

        # 3.合并规则命中、凭证校验结论与 AI 风险点
        document_findings = build_document_findings(context)        # 凭证事实结论
        findings = merge_findings(
            build_rule_findings(context) + document_findings,       # 确定性结论优先
            [finding.model_dump() for finding in output.findings],
        )

        # 4.规则/凭证下限：确定性 high/mid 抬高分数与等级
        summary = output.summary.strip()[:1000]
        score, level, summary = apply_rule_floor(
            score=score,
            level=level,
            summary=summary,
            rule_hits=context.get("rule_hits", []),
        )
        score, level, summary = apply_document_floor(
            score=score,
            level=level,
            summary=summary,
            document_findings=document_findings,
        )

        # 5.引用校验：只保留本轮注入条款的原文连续片段
        validation = validate_citations(
            output.policy_citations,
            policy,
            max_citations=settings.POLICY_CITATION_MAX,
            min_quote_chars=settings.POLICY_CITATION_MIN_QUOTE_CHARS,
        )

        # 6.policy 类型的 finding 必须绑定一条有效引用，否则丢弃
        allowed_clause_ids = {citation["clause_id"] for citation in validation.citations}   # 有效引用编号
        verified_findings: list[dict] = []          # 通过校验的风险点
        unverified_policy_findings = 0              # 无有效引用的 policy finding 数
        for finding in findings:
            if finding.get("type") == "policy" and finding.get("clause_id") not in allowed_clause_ids:
                unverified_policy_findings += 1     # 计入审计
                continue                            # 无依据的制度断言直接丢弃
            verified_findings.append(finding)       # 保留通过校验的风险点
        findings = verified_findings                # 替换为过滤后的列表

        # 7.审计统计：补充未验证的 policy finding 数量
        citation_stats = dict(validation.stats)     # 复制统计，避免修改原对象
        citation_stats["unverified_policy_findings"] = unverified_policy_findings

        # 8.记录归一化结果（不打印单据内容）
        logger.info(
            "AI 预审完成 expense_no=%s score=%s level=%s findings=%s citations=%s policy=%s fallback=%s",
            context.get("expense", {}).get("expense_no"),
            score,
            level,
            len(findings),
            len(validation.citations),
            policy.version if policy else "",
            False,
        )

        return ReviewOutcome(
            risk_score=score,
            risk_level=level,
            summary=summary,
            findings=findings,
            policy_citations=validation.citations,
            is_fallback=False,
            model=result.model,
            prompt_version=self.prompt_version,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            raw_content=result.content,
            policy_version=policy.version if policy else "",
            retrieved_clause_ids=list(policy.clause_ids) if policy else [],
            citation_stats=citation_stats,
        )



# 规则兜底结果（LLM 不可用/超时/输出非法时使用）
def build_fallback_outcome(context: dict, *, reason: str = "") -> ReviewOutcome:
    """
    完全基于规则命中生成兜底结论
        :param context: 预审上下文
        :param reason: 降级原因（写入日志，不进结论）
        :return: ReviewOutcome（is_fallback=True）
    """
    rule_hits = context.get("rule_hits", [])
    levels = {hit.get("level") for hit in rule_hits}

    # 1.按最严重的规则级别确定分数与等级
    if "block" in levels:
        score, level = Decimal("80"), "high"
    elif "review" in levels:
        score, level = Decimal("60"), "mid"
    elif "warn" in levels:
        score, level = Decimal("30"), "low"
    else:
        score, level = Decimal("10"), "low"

    # 2.规则命中转 findings
    findings = merge_findings(build_rule_findings(context), [])

    # 3.总结：最多列 3 条规则提示
    messages = [str(hit.get("message") or "")[:200] for hit in rule_hits[:3]]
    if messages:
        summary = "规则兜底：" + "；".join(messages)
    else:
        summary = "未发现明显风险（规则兜底）"
    summary = summary[:1000]

    # 4.记录降级日志（原因只进日志）
    logger.warning(
        "AI 预审降级为规则兜底 expense_no=%s reason=%s",
        context.get("expense", {}).get("expense_no"), reason,
    )

    return ReviewOutcome(
        risk_score=score,
        risk_level=level,
        summary=summary,
        findings=findings,
        policy_citations=[],
        is_fallback=True,
        model="rules",
        prompt_version="fallback_v1",
        latency_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
        raw_content="",
    )
