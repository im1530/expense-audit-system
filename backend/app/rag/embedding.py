"""
本地 ONNX embedding 实现（中文/多语言，无网络依赖）
    为什么不用 chromadb 默认的 ONNX MiniLM：
        - 默认模型是英文 all-MiniLM-L6-v2，中文检索实测 Recall@3 只有 62.5%，不可用；
        - 本实现从本地目录加载模型，支持中文/多语言模型（默认 paraphrase-multilingual-MiniLM-L12-v2）。
    约定：
        - 模型目录需包含 model.onnx 与 tokenizer.json；
        - 向量做 L2 归一化，Chroma 集合使用 cosine 距离；
        - pooling 支持 mean（MiniLM 系）与 cls（BGE 系），由配置决定。
    边界：
        - 本模块只负责把文本转成向量，不做检索、不写库；
        - 模型文件不打进仓库，部署时预置到 POLICY_EMBEDDING_DIR（或默认缓存目录）。
"""
import logging
from pathlib import Path
from typing import Any

import numpy as np

from app.config import settings


logger = logging.getLogger(__name__)

# 默认模型目录（相对用户缓存目录，避免把大文件放进仓库）
DEFAULT_MODEL_NAME = "bge-small-zh-v1.5"

# 进程级单例
_embedding_function: Any = None



# 解析模型目录
def resolve_embedding_dir(path: str | None = None) -> Path:
    """
    解析 embedding 模型目录
        :param path: 覆盖路径；为空读配置，仍为空则用用户缓存目录
        :return: 绝对路径
    """
    value = settings.POLICY_EMBEDDING_DIR if path is None else path
    if value:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    return Path.home() / ".cache" / "expense_audit" / "embeddings" / DEFAULT_MODEL_NAME



# 本地 ONNX embedding 函数
class LocalOnnxEmbeddingFunction:
    """
    从本地 ONNX 模型生成句向量
        :ivar model_dir: 模型目录
        :ivar pooling: mean / cls
        :ivar max_length: 截断长度
    """

    def __init__(self, model_dir: str | Path, *, pooling: str = "mean", max_length: int = 256):
        """
        :param model_dir: 模型目录（含 model.onnx 与 tokenizer.json）
        :param pooling: 池化方式：mean（MiniLM）/ cls（BGE）
        :param max_length: 最大 token 长度
        """
        # 1.延迟导入：未装 onnxruntime/tokenizers 时不影响 BM25
        import onnxruntime as ort
        from tokenizers import Tokenizer

        # 2.保存配置并加载模型
        self.model_dir = Path(model_dir)
        self.pooling = pooling if pooling in ("mean", "cls") else "mean"
        self.max_length = max(8, int(max_length))

        model_path = self.model_dir / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(f"embedding 模型文件缺失: {self.model_dir}")

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_names = {item.name for item in self._session.get_inputs()}

        # 3.配置 tokenizer：截断 + 右侧 padding（padding 符号按模型词表取）
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        pad_id = self._tokenizer.token_to_id("<pad>")
        pad_token = "<pad>"
        if pad_id is None:
            pad_id = self._tokenizer.token_to_id("[PAD]")
            pad_token = "[PAD]"
        if pad_id is None:
            pad_id = 0
            pad_token = "[PAD]"
        self._pad_id = pad_id
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=pad_token)

    # ---- Chroma EmbeddingFunction 协议 ----
    @staticmethod
    def name() -> str:
        """
        embedding 函数名（Chroma 持久化配置用）
            :return: 固定名称
        """
        return "local_onnx"

    def default_space(self) -> str:
        """
        默认距离空间
            :return: cosine
        """
        return "cosine"

    def supported_spaces(self) -> list[str]:
        """
        支持的距离空间
            :return: 空间列表
        """
        return ["cosine", "l2", "ip"]

    def get_config(self) -> dict:
        """
        可序列化配置（写入 Chroma 集合元数据）
            :return: 配置字典
        """
        return {
            "model_dir": str(self.model_dir),
            "pooling": self.pooling,
            "max_length": self.max_length,
        }

    @classmethod
    def build_from_config(cls, config: dict) -> "LocalOnnxEmbeddingFunction":
        """
        从配置重建（Chroma 反序列化用）
            :param config: get_config 的输出
            :return: 实例
        """
        return cls(
            config["model_dir"],
            pooling=config.get("pooling", "mean"),
            max_length=int(config.get("max_length", 256)),
        )

    # ---- 向量化 ----
    def embed_documents(self, input: list[str]) -> list[np.ndarray]:
        """
        批量文档向量化（Chroma 1.x 协议）
            :param input: 文本列表
            :return: 向量列表
        """
        return self(input)

    def embed_query(self, input: list[str]) -> list[np.ndarray]:
        """
        查询向量化（Chroma 1.x 协议）
            :param input: 查询文本列表
            :return: 向量列表
        """
        return self(input)

    def __call__(self, input: list[str]) -> list[np.ndarray]:
        """
        把一批文本转成归一化向量
            :param input: 文本列表
            :return: 向量列表（每个元素 float32 ndarray）
        """
        # 1.空输入直接返回
        texts = [str(item or "") for item in input]
        if not texts:
            return []

        # 2.批量编码并组装 ONNX 输入
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([item.ids for item in encodings], dtype=np.int64)
        attention_mask = np.array([item.attention_mask for item in encodings], dtype=np.int64)
        feeds = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        # 3.前向计算
        last_hidden_state = self._session.run(None, feeds)[0]

        # 4.池化：mean（按 mask 加权）/ cls（首个 token）
        if self.pooling == "cls":
            pooled = last_hidden_state[:, 0]
        else:
            expanded = np.expand_dims(attention_mask, -1)
            pooled = np.sum(last_hidden_state * expanded, axis=1) / np.clip(
                expanded.sum(axis=1), 1e-9, None
            )

        # 5.L2 归一化，便于直接用余弦相似度
        norm = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
        normalized = pooled / norm
        return [row.astype(np.float32) for row in normalized]



# 创建 embedding 函数
def build_embedding_function() -> LocalOnnxEmbeddingFunction:
    """
    获取进程级 embedding 函数（本地 ONNX 模型）
        :return: LocalOnnxEmbeddingFunction 实例
    """
    global _embedding_function
    # 1.已创建：直接复用，避免重复加载模型
    if _embedding_function is not None:
        return _embedding_function

    # 2.首次创建：加载本地模型
    model_dir = resolve_embedding_dir()
    logger.info("加载本地 embedding 模型 dir=%s pooling=%s", model_dir, settings.POLICY_EMBEDDING_POOLING)
    _embedding_function = LocalOnnxEmbeddingFunction(
        model_dir,
        pooling=settings.POLICY_EMBEDDING_POOLING,
        max_length=settings.POLICY_EMBEDDING_MAX_LENGTH,
    )
    return _embedding_function



# 重置缓存（测试/换模型用）
def reset_embedding_cache() -> None:
    """
    清空进程级 embedding 函数
        :return: 无
    """
    global _embedding_function
    _embedding_function = None
