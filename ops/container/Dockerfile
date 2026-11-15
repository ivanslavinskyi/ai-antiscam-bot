# Both stages use Debian bookworm. Keep the exact base digests in release records.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG POSTGRES_IMAGE=postgres:17.12-bookworm@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536
FROM ${PYTHON_IMAGE} AS python
FROM ${POSTGRES_IMAGE} AS runtime

# The PostgreSQL base supplies matching pg_dump/pg_restore and libpq 17.
# No PostgreSQL entrypoint or server is started in the application image.
COPY --from=python /usr/local/ /usr/local/
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libbz2-1.0 libffi8 libgdbm6 libgdbm-compat4 \
       liblzma5 libncursesw6 libreadline8 libsqlite3-0 libssl3 zlib1g \
       tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 antiscam \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent antiscam

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/lib/postgresql/17/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && /opt/venv/bin/python -m pip check
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY ops/*.py ./ops/

ARG APP_REVISION=unknown
LABEL org.opencontainers.image.title="AntiScam V2" \
      org.opencontainers.image.revision="${APP_REVISION}"
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT []
CMD ["python", "-m", "app.main"]
