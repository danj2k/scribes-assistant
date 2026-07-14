FROM python:3.11-slim

# Install libopus — py-cord loads it via ctypes at runtime to decode
# incoming Opus audio to PCM for WaveSink. The slim image omits it.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

# Install shared package first (for caching)
WORKDIR /app
COPY shared/ /app/shared/
RUN pip install --no-cache-dir /app/shared

# Copy bot code
COPY bot/ /app/bot/
RUN pip install --no-cache-dir -r /app/bot/requirements.txt

# Create data directories
RUN mkdir -p /data/recordings /data/transcripts /data/logs /data/models

# Set PYTHONPATH to include project root for shared module imports
ENV PYTHONPATH=/app

WORKDIR /app/bot

CMD ["python", "main.py"]
