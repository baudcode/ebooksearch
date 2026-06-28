# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EBOOK_DIR=/data/books \
    DB_PATH=/data/index.db

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Persistent state lives under /data — bind-mount your ebook folder there.
VOLUME ["/data"]
RUN mkdir -p /data/books

EXPOSE 8000

CMD ["uvicorn", "ebooksearch.main:app", "--host", "0.0.0.0", "--port", "8000"]
