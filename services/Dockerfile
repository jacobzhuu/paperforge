FROM public.ecr.aws/docker/library/node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS pi-runtime
WORKDIR /pi
COPY services/pi/package.json services/pi/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY services/pi/agent.mjs ./

FROM public.ecr.aws/docker/library/python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

RUN pip install --no-cache-dir "uv==0.9.16"

COPY pyproject.toml uv.lock README.md alembic.ini /app/
COPY packages /app/packages
COPY services/api /app/services/api
COPY services/worker /app/services/worker
# uv parses every workspace member even when syncing only the API dependency closure.
COPY services/texd/pyproject.toml /app/services/texd/pyproject.toml
COPY services/visuald/pyproject.toml /app/services/visuald/pyproject.toml

# paperforge-api already depends on paperforge-worker, so this installs both application
# entrypoints and their shared workspace packages without visuald's heavy rendering stack.
RUN uv sync --frozen --no-dev --no-editable --package paperforge-api

# Models form a separate cached stage so ordinary source edits do not redownload them.
FROM public.ecr.aws/docker/library/python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS retrieval-models
RUN pip install --no-cache-dir "fastembed==0.8.0"
COPY packages/retrieval/retrieval/models.py /tmp/download_models.py
RUN FASTEMBED_CACHE_PATH=/models python /tmp/download_models.py
RUN chmod -R a+rX /models

FROM public.ecr.aws/docker/library/python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime-common

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/app/models
WORKDIR /app
COPY --from=retrieval-models /models /app/models

RUN groupadd --gid 10001 paperforge \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent paperforge

FROM runtime-common AS worker-runtime
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::https::Timeout=30 install -y --no-install-recommends pandoc libseccomp2 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-common AS api
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 alembic.ini /app/alembic.ini
COPY --chown=10001:10001 packages/db/migrations /app/packages/db/migrations
USER 10001:10001
CMD ["paperforge-api"]

FROM worker-runtime AS worker
COPY --from=pi-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-runtime --chown=10001:10001 /pi /app/pi
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 alembic.ini /app/alembic.ini
COPY --chown=10001:10001 packages/db/migrations /app/packages/db/migrations
USER 10001:10001
CMD ["arq", "paperforge_worker.worker.WorkerSettings"]
