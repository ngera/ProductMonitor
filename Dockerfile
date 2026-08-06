# syntax=docker/dockerfile:1
#
# ProductMonitor — container image.
#
# Bundles the pipeline CLI (`python -m pipeline.run`), the local admin webui
# (`python -m webui.app`), and every optional dep (matplotlib for digest v2
# charts, sentence-transformers + torch for the persistent-issue stage).
# Uses CPU-only torch wheels (~200MB) instead of the default GPU wheels
# (~1GB with bundled CUDA libraries) since a container isn't going to get
# GPU access anyway. Net image size: ~1.2GB uncompressed. Strip matplotlib
# / sentence-transformers from requirements.txt to shave another ~500MB if
# you don't need digest v2's charts or cross-week clustering.
#
# Build:   docker build -t product-monitor:latest .
# Run UI:  docker compose up
# Run CLI: docker compose run --rm app python -m pipeline.run --product <slug>

FROM python:3.11-slim AS runtime

# System deps.
#   build-essential — a few transitive deps still compile C extensions on
#     ARM (torch ships wheels for x86_64 but not always for aarch64).
#   libgomp1 — torch's OpenMP runtime dependency on slim images.
#   curl — used by the compose healthcheck.
# Kept in one apt-get layer so a code change doesn't retrigger the download.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Running uvicorn as root is a real footgun since it reads
# .env and writes to bind-mounted volumes.
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install Python deps in an isolated layer so ordinary source edits don't
# retrigger the slow torch download.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    # Install torch from the CPU-only wheel index BEFORE the general
    # requirements install. Two reasons:
    #   1. The default `pip install sentence-transformers` pulls the GPU
    #      torch wheel with bundled CUDA libraries (~800MB extra), useless
    #      in a container without GPU passthrough.
    #   2. On Apple Silicon (linux/arm64) the default index only has the
    #      manylinux2014_x86_64 GPU wheel — build fails. The CPU index
    #      publishes real aarch64 wheels for both Linux and macOS builders.
    # After this, requirements.txt's sentence-transformers dep sees torch
    # already satisfied and reuses the CPU wheel.
    && pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch \
    && pip install --no-cache-dir -r requirements.txt

# Copy source last — .dockerignore keeps this to just the runtime bits.
COPY --chown=appuser:appuser . .

# The pipeline writes to data/ and reports/; the wizard writes to products/.
# These are bind-mounted from the host in docker-compose.yml so nothing is
# lost when the container is recreated; the mkdir here just guarantees the
# mount targets exist inside the image for one-off `docker run` usage too.
RUN mkdir -p /app/data /app/reports /app/products \
    && chown -R appuser:appuser /app/data /app/reports /app/products

USER appuser

# webui/app.py binds 127.0.0.1 by default (its local-only security posture);
# --host 0.0.0.0 lets Docker port-forward reach it. Port 8766 matches
# webui/app.py's native default so URLs printed in the app's startup log
# match what users navigate to via the host port map.
EXPOSE 8766

# Default: the admin web UI. Override CMD for the pipeline or demo:
#   docker compose run --rm app python -m pipeline.run --product windows
#   docker compose run --rm app python -m pipeline.demo
CMD ["python", "-m", "webui.app", "--host", "0.0.0.0", "--port", "8766"]
