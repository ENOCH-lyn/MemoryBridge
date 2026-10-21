FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMBRIDGE_DB=/data/membridge.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY membridge ./membridge

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 membridge \
    && mkdir -p /data \
    && chown -R membridge:membridge /data
USER membridge

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)" || exit 1

CMD ["membridge", "--db", "/data/membridge.db", "hub", "--host", "0.0.0.0", "--port", "8787"]
