FROM python:3.11-slim

# Install shared package first (for caching)
WORKDIR /app
COPY shared/ /app/shared/
RUN pip install --no-cache-dir /app/shared

# Copy bot code
COPY bot/ /app/bot/
COPY config.yaml* /app/
RUN pip install --no-cache-dir -r /app/bot/requirements.txt

# Create data directories
RUN mkdir -p /data/recordings /data/transcripts /data/logs /data/models

# Set PYTHONPATH to include project root for shared module imports
ENV PYTHONPATH=/app

WORKDIR /app/bot

CMD ["python", "main.py"]
