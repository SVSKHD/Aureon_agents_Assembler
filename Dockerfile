# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
# Proxies whose X-Forwarded-For uvicorn trusts (login rate limits key on client IP).
# Override in .env with the Docker bridge gateway Nginx connects from (see deploy/README.md).
ENV FORWARDED_ALLOW_IPS=127.0.0.1
# Fixed UID/GID so file ownership on /mnt/spasta-data is predictable.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid ${APP_GID} aureon \
 && useradd --system --uid ${APP_UID} --gid aureon --no-create-home --shell /usr/sbin/nologin aureon
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=aureon:aureon app ./app
COPY --chown=aureon:aureon alembic ./alembic
COPY --chown=aureon:aureon alembic.ini ./
USER aureon
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "--factory", "app.main:app_factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "20"]

# --- coding worker (opt-in) --------------------------------------------------------------
# docker compose --profile coding build coding-worker
# git for worktrees, Node for the frontend's test commands. Tests run with the repo's own
# node_modules / .venv linked in from the checkout, so nothing is installed at task time.
FROM runtime AS coding
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git nodejs npm ca-certificates \
 && rm -rf /var/lib/apt/lists/*
USER aureon
CMD ["python", "-m", "app.workers.coding"]

# --- imaging worker (opt-in) -------------------------------------------------------------
# docker compose --profile imaging build imaging-worker
# Installs the existing /opt/image-agent's Python dependencies (CPU wheels) so its
# entrypoint can run inside this container. Code and models stay in /opt/image-agent,
# bind-mounted read-only at runtime; nothing is downloaded at runtime.
FROM runtime AS imaging
ARG IMAGE_AGENT_REQUIREMENTS=requirements.txt
ARG PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
USER root
COPY --from=imageagent ${IMAGE_AGENT_REQUIREMENTS} /tmp/image-agent-requirements.txt
RUN pip install --no-cache-dir --extra-index-url ${PIP_EXTRA_INDEX_URL} \
      -r /tmp/image-agent-requirements.txt \
 && rm /tmp/image-agent-requirements.txt
USER aureon
CMD ["python", "-m", "app.workers.imaging"]
