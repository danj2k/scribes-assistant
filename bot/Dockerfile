FROM python:3.11-slim

# Install libopus and git. libopus is loaded by py-cord via ctypes at
# runtime to decode incoming Opus audio to PCM for WaveSink — the slim
# image omits it. git is needed to pip-install py-cord from the PR #3159
# branch (git+https://...) until it merges and a release ships.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 git \
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
