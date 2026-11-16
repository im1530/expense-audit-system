# 后端镜像：FastAPI + Celery（演示环境用 BM25，不安装 chromadb）
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
# 演示环境不装 chromadb（体积大、需要模型）；生产需要向量检索时再装
RUN grep -v '^chromadb' requirements.txt > /tmp/requirements-docker.txt \
    && pip install -r /tmp/requirements-docker.txt

# 只复制运行所需代码
COPY app ./app
COPY scripts ./scripts

# 附件目录（compose 会挂卷）
RUN mkdir -p /data/uploads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
