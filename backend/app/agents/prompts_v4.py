"""
Prompt v4 兼容入口
    v4 已成为线上默认 Prompt，具体内容维护在 app/agents/prompts.py：
        - PROMPT_VERSION = risk_review_v4_1_citation_guard
        - SYSTEM_PROMPT  = v3 + 【判断尺度（v4 校准）】 + 【制度引用校准（v4.1）】
    本文件保留 SYSTEM_PROMPT_V4 别名，供评测脚本和历史代码引用，避免重复维护两份 Prompt。
"""
from app.agents.prompts import (  # noqa: F401
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_policy_context,
    build_user_prompt,
)



# 兼容旧引用
SYSTEM_PROMPT_V4 = SYSTEM_PROMPT
