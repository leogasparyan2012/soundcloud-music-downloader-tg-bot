FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot.py ./
COPY soundcloud_bot ./soundcloud_bot

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/downloads \
    && chown -R bot:bot /app

USER bot

CMD ["python", "bot.py"]
