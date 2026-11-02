FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/data \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY setup.py VERSION README.md LICENSE MANIFEST.in ./
COPY fin_agent ./fin_agent
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/fin-agent \
    && chown -R app:app /data

USER app
VOLUME ["/data"]
ENTRYPOINT ["fin-agent"]
