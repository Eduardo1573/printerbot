FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Ghostscript rasterises PDFs to PWG Raster: the M129–M134 cannot print PDF directly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --create-home --home-dir /home/bot bot
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY app ./app
RUN chmod 0755 docker-entrypoint.sh \
 && mkdir -p /var/tmp/printerbot \
 && chown bot:bot /var/tmp/printerbot \
 && chmod 0700 /var/tmp/printerbot

# ---------------------------------------------------------------- tests
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER bot
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------- runtime
FROM base AS runtime
USER bot
EXPOSE 2525
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
