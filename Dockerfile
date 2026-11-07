ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIN_AGENT_ENGINE=harness \
    XDG_CONFIG_HOME=/data \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt requirements-harness.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-harness.txt
COPY setup.py VERSION README.md LICENSE MANIFEST.in ./
COPY fin_agent ./fin_agent
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && (id -u app >/dev/null 2>&1 || useradd --create-home --uid 10001 app) \
    && mkdir -p /data/fin-agent \
    && chown -R app:app /data

USER app
VOLUME ["/data"]
ENTRYPOINT ["fin-agent"]
