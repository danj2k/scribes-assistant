FROM python:3.11-slim

# Install system dependencies for sherpa-onnx and soundfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy shared package first (for caching)
COPY shared/ /app/shared/

# Install shared package
RUN cd /app/shared && pip install --no-cache-dir -e .

# Copy transcriber code
COPY transcriber/ /app/transcriber/

# Install transcriber dependencies
RUN cd /app/transcriber && pip install --no-cache-dir -r requirements.txt

# Create data directories for model and recordings
RUN mkdir -p /app/data/models/whisper-small \
    && mkdir -p /app/data/recordings \
    && mkdir -p /app/data/transcripts \
    && mkdir -p /app/data/logs

# Set PYTHONPATH to include project root
ENV PYTHONPATH=/app

# Set working directory to transcriber
WORKDIR /app/transcriber

# Health check — verify the process is running
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Run the transcriber
CMD ["python", "main.py"]
