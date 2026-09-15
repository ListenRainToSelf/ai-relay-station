# 本地 AI 中转站 · 容器镜像
# 群晖 / 威联通 / Unraid 等 NAS 上最省事的部署方式：容器里跑无头服务，数据挂到卷。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AIRELAY_DATA_DIR=/data \
    AIRELAY_MODE=server \
    AIRELAY_HOST=0.0.0.0 \
    AIRELAY_PORT=8000

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY airelay/ ./airelay/
COPY run.py README.md ./

# 数据目录：数据库、日志、secrets.json 都在这里
VOLUME ["/data"]
EXPOSE 8000

# 用非 root 运行；镜像内没有别的服务，不会冲突
RUN useradd --create-home --uid 10001 airelay \
    && mkdir -p /data \
    && chown -R airelay:airelay /data /app
USER airelay

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "airelay", "--mode", "server"]
