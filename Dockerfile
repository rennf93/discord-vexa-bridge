# Pinned to 3.11: the bot uses the stdlib `audioop` module, removed in 3.13.
# Do NOT bump past 3.11 (dependabot is configured to skip minor/major python bumps).
FROM python:3.11-slim

# libopus is needed to DECODE incoming Discord voice.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY dave_voice ./dave_voice
CMD ["python", "bot.py"]
