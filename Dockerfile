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

# ---- rootfs stage: assemble exactly what will ship ----------------------------
FROM python:3.13-slim AS rootfs

# Security updates for OS packages the base image has not picked up yet, and no
# package manager at runtime: the app never installs anything, and pip's
# vendored libraries would only add attack surface (and scanner findings).
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 leyline \
 && useradd --system --uid 10001 --gid leyline --no-create-home leyline \
 && mkdir -p /app

COPY --from=build /opt/venv /opt/venv
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel \
 && /opt/venv/bin/python -m pip uninstall -y pip setuptools wheel
COPY --chown=root:root corpus /app/corpus

# ---- runtime stage: one flat layer, non-root ---------------------------------
# Copying the finished filesystem into an empty image leaves a single layer, so
# files deleted above (pip and its vendored libraries) are not shipped in a
# lower layer where image scanners would still find them.
FROM scratch AS runtime
COPY --from=rootfs / /

ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin" \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEYLINE_ENV=production \
    CORPUS_DIR=/app/corpus

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# A streamed answer holds a thread for its whole length but is I/O-bound, so
# threads are cheap: 2 workers x 16 threads = 32 concurrent answers per container
# (see docs/LOAD_TEST.md). Keep-alive spares idle clients a reconnect.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "16", \
     "--keep-alive", "15", \
     "--timeout", "60", "--access-logfile", "-", "--no-control-socket", \
     "leyline.app:create_app()"]
