"""
制度文档加载与校验（Markdown → PolicyDocument）
    文档格式：
        - 文档级元数据放在 <!-- policy:doc ... --> 中；
        - 每个条款由 <!-- policy:clause ... --> 开始、<!-- /policy:clause --> 结束；
        - 条款正文夹在两个标签之间，逐字保留；
        - section 默认取最近的 "## " 标题，也可以在条款元数据里显式覆盖。
    校验原则：
        - 结构错误直接抛 PolicyDocError，由上层降级为"无制度模式"，绝不阻断审批；
        - 条款编号唯一、版本一致、正文非空、applies_to 取值合法；
        - 解析结果只读缓存，测试可显式 reset。
"""
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.config import settings
from app.rag.schema import (
    ALLOWED_EXPENSE_TYPES,
    ALLOWED_ROLES,
    ALLOWED_RULE_TYPES,
    PolicyClause,
    PolicyDocument,
)



logger = logging.getLogger(__name__)

# backend 目录：app/rag/loader.py 往上三层
BACKEND_DIR = Path(__file__).resolve().parents[2]

# 文档级元数据块
DOC_PATTERN = re.compile(r"<!--\s*policy:doc\s*(.*?)\s*-->", re.S)

# 条款块：元数据 + 正文 + 结束标签
CLAUSE_PATTERN = re.compile(
    r"<!--\s*policy:clause\s*(.*?)\s*-->\s*(.*?)\s*<!--\s*/policy:clause\s*-->",
    re.S,
)

# 二级标题：用于推导条款所属章节
SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.M)

# 条款编号：如 5.2.1
CLAUSE_ID_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")

# 制度标识：小写字母、数字、下划线
DOC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# 文档元数据允许的键
DOC_META_KEYS = {"doc_id", "title", "version"}

# 条款元数据允许的键
CLAUSE_META_KEYS = {"clause_id", "section", "version", "keywords", "applies_to"}

# 进程级文档缓存
_document: PolicyDocument | None = None



# 制度文档错误
class PolicyDocError(Exception):
    """
    制度文档解析/校验错误
        上层捕获后降级为"无制度模式"，不影响预审主流程。
    """



# 解析 key: value 元数据块
def _parse_metadata(block: str, *, label: str) -> dict[str, str]:
    """
    把多行 key: value 元数据解析成字典
        :param block: 元数据块文本
        :param label: 出错时的定位说明（doc/clause）
        :return: 键值字典
        :raises PolicyDocError: 缺少冒号、重复键或空键
    """
    data: dict[str, str] = {}                       # 元数据结果
    for raw_line in block.splitlines():
        line = raw_line.strip()                     # 去首尾空白
        if not line or line.startswith("#"):
            continue                                # 跳过空行与注释
        if ":" not in line:
            raise PolicyDocError(f"{label} 元数据行缺少冒号: {line}")
        key, value = line.split(":", 1)             # 只按第一个冒号拆
        key = key.strip().lower()                   # 键统一小写
        value = value.strip()                       # 值去首尾空白
        if not key:
            raise PolicyDocError(f"{label} 元数据存在空键")
        if key in data:
            raise PolicyDocError(f"{label} 元数据键重复: {key}")
        data[key] = value                           # 记录键值
    return data



# 解析 applies_to
def _parse_applies_to(value: str, *, clause_id: str) -> dict:
    """
    解析适用范围表达式
        格式：types=transportation; roles=employee,admin; min=1000; max=19999.99
        未出现的维度表示不限，等号右侧的 * 也表示不限。
        :param value: applies_to 原始值
        :param clause_id: 条款编号（错误定位用）
        :return: 含 expense_types/roles/rule_types/min_amount/max_amount 的字典
        :raises PolicyDocError: 语法错误或取值不在白名单
    """
    result = {
        "expense_types": (),        # 适用费用大类
        "roles": (),               # 适用角色
        "rule_types": (),          # 适用规则类型
        "min_amount": None,         # 金额下限
        "max_amount": None,         # 金额上限
    }
    text = (value or "").strip()                    # 去首尾空白
    if not text:
        return result                               # 空值表示不限

    # 1.按分号拆成 key=value
    for part in text.split(";"):
        item = part.strip()                         # 单个条件
        if not item:
            continue                                # 跳过空段
        key, separator, raw_value = item.partition("=")   # 拆分键值
        if not separator:
            raise PolicyDocError(f"条款 {clause_id} 的 applies_to 片段缺少等号: {item}")
        key = key.strip().lower()                   # 键统一小写
        raw_value = raw_value.strip()               # 值去空白

        # 2.列表型条件
        if key in ("types", "expense_types", "roles", "rule_types"):
            values = () if raw_value in ("", "*") else tuple(
                value.strip() for value in raw_value.split(",") if value.strip()
            )
            if key in ("types", "expense_types"):
                unknown = [value for value in values if value not in ALLOWED_EXPENSE_TYPES]
                if unknown:
                    raise PolicyDocError(f"条款 {clause_id} 的适用费用大类非法: {unknown}")
                result["expense_types"] = values
            elif key == "roles":
                unknown = [value for value in values if value not in ALLOWED_ROLES]
                if unknown:
                    raise PolicyDocError(f"条款 {clause_id} 的适用角色非法: {unknown}")
                result["roles"] = values
            else:
                unknown = [value for value in values if value not in ALLOWED_RULE_TYPES]
                if unknown:
                    raise PolicyDocError(f"条款 {clause_id} 的适用规则类型非法: {unknown}")
                result["rule_types"] = values
            continue

        # 3.金额型条件
        if key in ("min", "min_amount", "max", "max_amount"):
            if raw_value in ("", "*"):
                amount = None                       # * 表示不限
            else:
                try:
                    amount = Decimal(raw_value)     # 转 Decimal，避免浮点误差
                except InvalidOperation as exc:
                    raise PolicyDocError(f"条款 {clause_id} 的金额条件非法: {raw_value}") from exc
            if key in ("min", "min_amount"):
                result["min_amount"] = amount
            else:
                result["max_amount"] = amount
            continue

        # 4.未知条件
        raise PolicyDocError(f"条款 {clause_id} 的 applies_to 键非法: {key}")

    # 5.金额区间合法性
    min_amount = result["min_amount"]
    max_amount = result["max_amount"]
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise PolicyDocError(f"条款 {clause_id} 的金额区间下限大于上限")

    return result



# 构造单条条款
def _build_clause(
    meta: dict[str, str],
    body: str,
    *,
    section: str,
    doc_version: str,
) -> PolicyClause:
    """
    校验并构造一条 PolicyClause
        :param meta: 条款元数据
        :param body: 条款正文
        :param section: 从标题推导的章节
        :param doc_version: 文档版本
        :return: PolicyClause
        :raises PolicyDocError: 字段缺失或非法
    """
    # 1.必填字段
    clause_id = meta.get("clause_id", "").strip()           # 条款编号
    if not clause_id:
        raise PolicyDocError("条款缺少 clause_id")
    if not CLAUSE_ID_PATTERN.match(clause_id):
        raise PolicyDocError(f"条款编号格式非法: {clause_id}")

    # 2.版本一致性
    version = meta.get("version", "").strip()               # 条款版本
    if not version:
        raise PolicyDocError(f"条款 {clause_id} 缺少 version")
    if version != doc_version:
        raise PolicyDocError(f"条款 {clause_id} 的版本 {version} 与文档版本 {doc_version} 不一致")

    # 3.章节：元数据优先，否则用最近的二级标题
    clause_section = meta.get("section", "").strip() or section.strip()   # 所属章节
    if not clause_section:
        raise PolicyDocError(f"条款 {clause_id} 缺少 section（且上方没有二级标题）")

    # 4.正文逐字保留
    text = (body or "").strip()                             # 条款原文
    if len(text) < 10:
        raise PolicyDocError(f"条款 {clause_id} 正文过短或为空")

    # 5.关键词
    raw_keywords = meta.get("keywords", "").strip()         # 关键词原始值
    keywords = tuple(
        dict.fromkeys(
            keyword.strip() for keyword in raw_keywords.split(",") if keyword.strip()
        )
    )
    if not keywords:
        raise PolicyDocError(f"条款 {clause_id} 缺少 keywords")

    # 6.适用范围
    applies_to = _parse_applies_to(meta.get("applies_to", ""), clause_id=clause_id)

    # 7.返回不可变条款对象
    return PolicyClause(
        clause_id=clause_id,
        section=clause_section,
        text=text,
        keywords=keywords,
        expense_types=tuple(applies_to["expense_types"]),
        roles=tuple(applies_to["roles"]),
        rule_types=tuple(applies_to["rule_types"]),
        min_amount=applies_to["min_amount"],
        max_amount=applies_to["max_amount"],
        version=version,
    )



# 解析制度文档文本
def parse_policy_document(text: str, *, source_path: str = "") -> PolicyDocument:
    """
    把 Markdown 制度文本解析成 PolicyDocument
        :param text: 文档全文
        :param source_path: 源文件路径（审计/排障用）
        :return: 解析后的制度文档
        :raises PolicyDocError: 结构不合法
    """
    # 1.文档元数据必须存在且只有一个
    doc_blocks = DOC_PATTERN.findall(text)                   # 所有文档元数据块
    if len(doc_blocks) != 1:
        raise PolicyDocError(f"制度文档必须且只能有一个 policy:doc 块，实际 {len(doc_blocks)} 个")
    doc_meta = _parse_metadata(doc_blocks[0], label="doc")   # 解析文档元数据
    unknown_doc_keys = set(doc_meta) - DOC_META_KEYS         # 未知文档键
    if unknown_doc_keys:
        raise PolicyDocError(f"制度文档存在未知元数据键: {sorted(unknown_doc_keys)}")

    # 2.文档级必填字段
    doc_id = doc_meta.get("doc_id", "").strip()              # 制度标识
    title = doc_meta.get("title", "").strip()                # 制度名称
    version = doc_meta.get("version", "").strip()            # 制度版本
    if not doc_id or not DOC_ID_PATTERN.match(doc_id):
        raise PolicyDocError(f"制度 doc_id 非法: {doc_id!r}")
    if not title:
        raise PolicyDocError("制度 title 不能为空")
    if not version:
        raise PolicyDocError("制度 version 不能为空")

    # 3.条款开始/结束标签必须成对
    open_count = text.count("<!-- policy:clause")            # 开始标签数
    close_count = text.count("<!-- /policy:clause -->")      # 结束标签数
    if open_count != close_count:
        raise PolicyDocError(f"条款标签不成对：开始 {open_count} 个，结束 {close_count} 个")

    # 4.收集标题位置，用于推导条款所属章节
    sections = [
        (match.start(), match.group(1).strip())             # (标题位置, 标题文本)
        for match in SECTION_PATTERN.finditer(text)
    ]

    # 5.逐条解析
    clauses: list[PolicyClause] = []                         # 条款列表
    seen_ids: set[str] = set()                               # 已出现的条款编号
    for match in CLAUSE_PATTERN.finditer(text):
        meta = _parse_metadata(match.group(1), label="clause")   # 条款元数据
        unknown_keys = set(meta) - CLAUSE_META_KEYS              # 未知条款键
        if unknown_keys:
            raise PolicyDocError(f"条款存在未知元数据键: {sorted(unknown_keys)}")

        # 5.1 找到该条款之前最近的二级标题
        section = ""                                        # 所属章节
        for position, heading in sections:
            if position < match.start():
                section = heading                           # 持续覆盖为最近的标题
            else:
                break

        # 5.2 构造并校验
        clause = _build_clause(
            meta,
            match.group(2),
            section=section,
            doc_version=version,
        )
        if clause.clause_id in seen_ids:
            raise PolicyDocError(f"条款编号重复: {clause.clause_id}")
        seen_ids.add(clause.clause_id)                      # 记录编号
        clauses.append(clause)                              # 收入结果

    # 6.至少一条条款
    if not clauses:
        raise PolicyDocError("制度文档没有解析到任何条款")

    # 7.返回不可变文档对象
    return PolicyDocument(
        doc_id=doc_id,
        title=title,
        version=version,
        clauses=tuple(clauses),
        source_path=source_path,
    )



# 解析制度文件路径
def resolve_policy_file() -> Path:
    """
    根据配置解析当前制度文件的绝对路径
        POLICY_DIR 为相对路径时，相对 backend 目录解析。
        :return: 制度文件绝对路径
        :raises PolicyDocError: 路径越界
    """
    root = Path(settings.POLICY_DIR)                        # 制度目录
    if not root.is_absolute():
        root = BACKEND_DIR / root                           # 相对 backend 目录
    root = root.resolve()                                   # 绝对化
    target = (root / settings.POLICY_FILE).resolve()        # 目标文件

    # 越界校验：文件必须仍在制度目录内
    try:
        target.relative_to(root)                            # 必须位于根目录下
    except ValueError as exc:
        raise PolicyDocError(f"制度文件路径越界: {target}") from exc
    return target



# 加载制度文件
def load_policy_document(path: str | Path | None = None) -> PolicyDocument:
    """
    从磁盘读取并解析制度文件
        :param path: 文件路径；不传则按配置解析
        :return: 解析后的制度文档
        :raises PolicyDocError: 文件不存在或解析失败
    """
    target = Path(path) if path is not None else resolve_policy_file()   # 目标文件
    if not target.is_file():
        raise PolicyDocError(f"制度文件不存在: {target}")
    try:
        text = target.read_text(encoding="utf-8")            # 读取全文
    except OSError as exc:
        raise PolicyDocError(f"制度文件读取失败: {target}") from exc
    return parse_policy_document(text, source_path=str(target))



# 获取缓存文档
def get_policy_document() -> PolicyDocument:
    """
    获取进程级缓存的制度文档（首次调用时加载）
        :return: 制度文档
        :raises PolicyDocError: 加载失败
    """
    global _document
    if _document is None:
        _document = load_policy_document()                   # 首次加载并缓存
        logger.info(
            "制度文档加载完成 doc_id=%s version=%s clauses=%s",
            _document.doc_id,
            _document.version,
            len(_document.clauses),
        )
    return _document



# 清空缓存（测试/热更新用）
def reset_policy_cache() -> None:
    """
    清空进程级制度缓存
        测试用例切换临时文档时调用；生产环境通常不需要。
        :return: 无
    """
    global _document
    _document = None
