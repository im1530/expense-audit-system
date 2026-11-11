"""
轻量文本处理（无第三方分词/向量依赖）
    为什么不用 jieba：
        - 当前环境无法稳定安装第三方包；
        - 制度条款短、关键词由人工维护，中文按"字符 bigram"切分已足够召回；
        - bigram 对"高铁票""业务招待餐"这类费用词更稳，不依赖词典。
    能力边界：
        - 只做归一化、切词、同义词扩展、引用比对归一化；
        - 不做语义理解、不做向量化，后续换 Embedding 时本模块仍可复用。
"""
import re
import unicodedata



# 中文连续片段：按 bigram 切分
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")

# 英文/数字/小数：保留金额与编号
ASCII_PATTERN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")

# 空白：引用比对时全部去掉
WHITESPACE_PATTERN = re.compile(r"\s+")

# 极少量停用词：避免"的/了/和"等单字进入查询
STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "在", "为", "是", "有", "无",
    "the", "a", "an", "of", "to", "and", "or", "is", "are",
}

# 同义词扩展：解决口语/书面语与制度用词的差异
SYNONYMS = {
    "打车": ("出租车", "网约车", "市内交通"),          # 打车 → 出租车/网约车
    "出租车": ("打车", "网约车"),                      # 出租车 → 打车/网约车
    "网约车": ("打车", "出租车"),                      # 网约车 → 打车/出租车
    "高铁": ("动车", "火车"),                          # 高铁 → 动车/火车
    "动车": ("高铁", "火车"),                          # 动车 → 高铁/火车
    "火车": ("高铁", "动车"),                          # 火车 → 高铁/动车
    "机票": ("飞机", "民航"),                          # 机票 → 飞机/民航
    "飞机": ("机票", "民航"),                          # 飞机 → 机票/民航
    "住宿": ("酒店",),                                 # 住宿 → 酒店
    "酒店": ("住宿",),                                 # 酒店 → 住宿
    "餐费": ("餐饮",),                                 # 餐费 → 餐饮
    "餐饮": ("餐费",),                                 # 餐饮 → 餐费
    "招待": ("业务招待",),                             # 招待 → 业务招待
    "业务招待": ("招待",),                             # 业务招待 → 招待
    "办公用品": ("办公费", "耗材"),                    # 办公用品 → 办公费/耗材
    "发票": ("票据",),                                 # 发票 → 票据
    "票据": ("发票",),                                 # 票据 → 发票
}



# 文本归一化
def normalize_text(value: str) -> str:
    """
    统一全半角、大小写与首尾空白
        只做"比较前"的归一化，不改变制度原文的存储内容。
        :param value: 原始文本
        :return: 归一化后的文本
    """
    text = unicodedata.normalize("NFKC", str(value or ""))   # 全角转半角
    return text.lower().strip()                              # 转小写并去首尾空白



# 切词
def tokenize(text: str) -> list[str]:
    """
    把文本切成 BM25 可用的词项
        规则：
            - 英文/数字/小数按整段保留；
            - 中文按字符 bigram 切分，单字保留单字；
            - 过滤停用词。
        :param text: 原始文本
        :return: 词项列表（保留顺序）
    """
    normalized = normalize_text(text)                       # 归一化文本
    tokens: list[str] = []                                  # 词项结果

    # 1.中文：连续片段切 bigram
    for match in CJK_PATTERN.finditer(normalized):
        run = match.group(0)                                # 中文连续片段
        if len(run) == 1:
            tokens.append(run)                              # 单字直接保留
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))   # 相邻两字

    # 2.英文/数字：整段保留，小数额外保留整数部分
    for match in ASCII_PATTERN.finditer(normalized):
        token = match.group(0)                              # 英文/数字片段
        tokens.append(token)                                # 完整词项
        if "." in token:
            tokens.append(token.split(".", 1)[0])           # 金额整数部分，如 2000.00 → 2000

    # 3.过滤停用词与空串
    return [token for token in tokens if token and token not in STOPWORDS]



# 同义词扩展
def expand_tokens(tokens: list[str]) -> list[str]:
    """
    对查询词做同义词扩展，提升口语化表述的召回
        :param tokens: 原始词项
        :return: 扩展后的词项（去重，保留首次出现顺序）
    """
    expanded: list[str] = []                                # 扩展结果
    seen: set[str] = set()                                  # 已出现词项
    for token in tokens:
        # 1.原词入结果
        if token not in seen:
            expanded.append(token)                          # 保留原词
            seen.add(token)                                 # 标记已出现
        # 2.同义词入结果
        for synonym in SYNONYMS.get(token, ()):
            if synonym not in seen:
                expanded.append(synonym)                    # 加入同义词
                seen.add(synonym)                           # 标记已出现
    return expanded



# 引用比对归一化
def normalize_for_match(value: str) -> str:
    """
    把文本归一化成"可直接做子串比对"的形式
        只去掉空白并统一全半角/大小写，不删标点、不改写文字：
        这样"原文摘录"仍能逐字校验，避免把改写过的引用误判为合法。
        :param value: 原始文本
        :return: 去空白后的归一化文本
    """
    text = unicodedata.normalize("NFKC", str(value or ""))   # 全角转半角
    text = text.lower()                                     # 转小写
    return WHITESPACE_PATTERN.sub("", text)                  # 去掉全部空白



# 去掉模型给引用自动加的外层引号
def strip_wrapping_quotes(value: str) -> str:
    """
    去掉引用两端成对的引号（模型常把摘录再包一层引号）
        只去两端，不改中间内容；避免因为多一对引号导致原文校验失败。
        :param value: 原始引用
        :return: 去掉外层引号后的文本
    """
    text = str(value or "").strip()                         # 去首尾空白
    pairs = (("“", "”"), ("\"", "\""), ("‘", "’"), ("'", "'"))   # 常见引号对
    changed = True                                          # 是否还在变化
    while changed and len(text) >= 2:
        changed = False                                     # 本轮默认不变
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()                   # 去掉一对引号
                changed = True                              # 标记有变化
                break
    return text
