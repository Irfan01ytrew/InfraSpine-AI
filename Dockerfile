# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Node runtime copied into the API image when INSTALL_BAILEYS=true so the
# in-process WhatsApp bridge (integrations/whatsapp-baileys/bridge.mjs) can run.
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS node-runtime

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    RAG_DATA_DIR=/var/lib/infraspine/data \
    LOG_DIR=/var/lib/infraspine/logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libglib2.0-0 libgomp1 libgl1 libsm6 libxext6 libxrender1 \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf 'Types: deb\nURIs: https://apt.postgresql.org/pub/repos/apt\nSuites: %s-pgdg\nArchitectures: %s\nComponents: main\nSigned-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n' \
        "$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")" "$(dpkg --print-architecture)" \
        > /etc/apt/sources.list.d/pgdg.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin infraspine \
    && mkdir -p /var/lib/infraspine/data /var/lib/infraspine/logs /var/lib/infraspine/backups \
    && chown -R infraspine:infraspine /var/lib/infraspine

WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY requirements-media.txt ./requirements-media.txt
ARG INSTALL_MEDIA_RUNTIME=false
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple torch==2.14.1 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir 'gunicorn==23.0.0' \
    && if [ "$INSTALL_MEDIA_RUNTIME" = "true" ]; then python -m pip install --no-cache-dir -r requirements-media.txt; fi
RUN python -m pip install --no-cache-dir --force-reinstall --no-deps \
        --index-url https://download.pytorch.org/whl/cpu torchvision==0.29.1+cpu
COPY requirements-document-tables.txt ./requirements-document-tables.txt
RUN python -m pip install --no-cache-dir -r requirements-document-tables.txt
COPY requirements-reporting.txt ./requirements-reporting.txt
RUN python -m pip install --no-cache-dir -r requirements-reporting.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
COPY docker/smoke/smoke_rag.py ./docker/smoke/smoke_rag.py
COPY --from=frontend-build /src/frontend/dist ./frontend/dist

ARG INSTALL_BAILEYS=false
COPY integrations/whatsapp-baileys ./integrations/whatsapp-baileys
RUN if [ "$INSTALL_BAILEYS" = "true" ]; then \
        mkdir -p /usr/local/node && \
        # Copy the Node 22 runtime from the official image (no apt Node package). \
        true; \
    fi
# Node binaries (optional): copied whenever INSTALL_BAILEYS=true
COPY --from=node-runtime /usr/local/bin/node /tmp/node-bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /tmp/node-bin/node_modules
RUN if [ "$INSTALL_BAILEYS" = "true" ]; then \
        cp /tmp/node-bin/node /usr/local/bin/node && \
        mkdir -p /usr/local/lib && \
        cp -a /tmp/node-bin/node_modules /usr/local/lib/node_modules && \
        ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
        chown -R infraspine:infraspine /app/integrations/whatsapp-baileys && \
        cd /app/integrations/whatsapp-baileys && npm ci --omit=dev --omit=optional; \
    fi && rm -rf /tmp/node-bin

COPY docker/api-entrypoint.sh /usr/local/bin/infraspine-entrypoint.sh
RUN chmod 755 /usr/local/bin/infraspine-entrypoint.sh \
    && chown -R infraspine:infraspine /app

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
# API_WORKERS > 1 needs RAG_INDEX_ROLE=auto: one process owns the vector index, the others serve from in-memory replicas.
# API_SERVER=gunicorn uses gunicorn + UvicornWorker; the default is uvicorn.
ENTRYPOINT ["/usr/local/bin/infraspine-entrypoint.sh"]
CMD ["api"]
