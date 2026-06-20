# py3.11: audioop is still present here (removed in 3.13).
FROM python:3.11-slim

# libopus is needed to DECODE incoming Discord voice.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir "py-cord[voice]" asyncpg aiohttp
COPY bot.py .
CMD ["python", "bot.py"]