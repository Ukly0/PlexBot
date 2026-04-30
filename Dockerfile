FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends unrar ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Create plexbot user (UID 1000 for media permissions)
RUN useradd -u 1000 -m plexbot || true \
    && mkdir -p /data/tdl \
    && chown -R plexbot:plexbot /data

USER plexbot

ENTRYPOINT ["python", "-m", "app.bot"]
