FROM python:3.11-slim

# build-essential is needed for compiling any pip packages without prebuilt
# wheels.  sherpa-onnx and soundfile both ship self-contained wheels —
# sherpa-onnx bundles libonnxruntime and libasound in its package directory,
# and soundfile bundles libsndfile in _soundfile_data/.  No system audio
# libraries are required.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy shared package first (for caching)
COPY shared/ /app/shared/

# Install shared package
RUN pip install --no-cache-dir /app/shared

# Copy transcriber code
COPY transcriber/ /app/transcriber/
# Copy scripts (model download, etc.)
COPY scripts/ /app/transcriber/scripts/

# Install transcriber dependencies and create data directories.
# Paths match the volume mount at /data in docker-compose.yml
# and the config defaults (database.path, lexicon.file, etc.).
RUN cd /app/transcriber && pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data/recordings \
    && mkdir -p /data/transcripts \
    && mkdir -p /data/logs \
    && mkdir -p /data/models

# Set PYTHONPATH to include project root
ENV PYTHONPATH=/app

# Set working directory to transcriber
WORKDIR /app/transcriber

# Health check — verify the transcriber is actively polling by checking
# the heartbeat file freshness.  The worker writes /data/transcriber.heartbeat
# after each poll cycle; if it's stale the process is stuck or crashed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python /app/transcriber/healthcheck.py

# Run the transcriber
CMD ["python", "main.py"]
