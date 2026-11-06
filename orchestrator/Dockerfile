# Build context: repository root.  docker build -f orchestrator/Dockerfile -t agent-platform/orchestrator:$VERSION .
ARG PYTHON_BASE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY orchestrator/pyproject.toml ./
COPY orchestrator/app ./app
# agent-runner (stdlib only) provides the tool loop for inline agents such as chat.
COPY sandbox/pyproject.toml /tmp/agent-runner/pyproject.toml
COPY sandbox/agent_runner /tmp/agent-runner/agent_runner
RUN pip install /tmp/agent-runner . && rm -rf /tmp/agent-runner && pip freeze > /app/requirements.lock

# Agent catalog and prompts are baked in; mount ./agents over /app/agents to customise without rebuilding.
COPY agents /app/agents

RUN useradd --system --uid 10001 --home /app agent && mkdir -p /app/config && chown -R agent /app
USER 10001

ENV AGENTS_DIR=/app/agents \
    PROJECTS_FILE=/app/config/projects.yaml \
    SECRETS_DIR=/run/secrets

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "--factory", "app.main:app_factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
