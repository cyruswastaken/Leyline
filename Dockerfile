# syntax=docker/dockerfile:1.7

# ---- build stage: resolve and build wheels --------------------------------
FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ---- runtime stage: no compilers, no build tooling, non-root --------------
FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEYLINE_ENV=production \
    CORPUS_DIR=/app/corpus \
    LOG_DIR=/var/log/leyline

RUN groupadd --system --gid 10001 leyline \
 && useradd --system --uid 10001 --gid leyline --no-create-home leyline \
 && mkdir -p /app /var/log/leyline \
 && chown leyline:leyline /var/log/leyline

COPY --from=build /opt/venv /opt/venv
COPY --chown=root:root corpus /app/corpus

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", \
     "--timeout", "60", "--access-logfile", "-", "leyline.app:create_app()"]
