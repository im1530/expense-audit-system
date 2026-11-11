"""
隐私与数据处理策略（Phase F）
    职责：
        1. 判断用户是否已对"发票影像发送给模型服务"完成当前版本授权；
        2. 统一授权提示文案，避免 API/服务层各写一套；
        3. 不读写数据库、不调用模型，只做纯策略判断。
    边界：
        - 授权只覆盖凭证影像；纯文本的规则/AI 风险预审不受影响；
        - 授权版本变化后旧授权自动失效，需要用户重新授权。
"""
from app.config import settings



# 未授权时的统一提示
AI_CONSENT_REQUIRED_MESSAGE = "尚未授权将发票/凭证影像发送给模型服务，请先在账号设置中完成授权"



# 判断用户是否已授权
def has_ai_consent(user) -> bool:
    """
    判断用户是否已完成当前版本的 AI 数据处理授权
        :param user: 用户对象（需有 ai_consent_at / ai_consent_version 属性）
        :return: 已授权 True
    """
    # 1.功能未要求授权：直接放行（仅开发/测试可关闭）
    if not settings.DOCUMENT_AI_CONSENT_REQUIRED:
        return True

    # 2.没有用户或没有授权时间：视为未授权
    if user is None or getattr(user, "ai_consent_at", None) is None:
        return False

    # 3.授权版本必须与当前条款版本一致，否则需要重新授权
    return str(getattr(user, "ai_consent_version", "") or "") == settings.DOCUMENT_AI_CONSENT_VERSION
