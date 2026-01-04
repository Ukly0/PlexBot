FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLEXBOT_HOME=/app
ENV TDL_HOME=/data/tdl

RUN apt-get update \
    && apt-get install -y --no-install-recommends unrar ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Runtime dirs (DB/TDL session). Aligns with TARGET_UID/GID=1000 in code.
RUN mkdir -p /data/tdl /data && useradd -u 1000 -m plexbot || true && chown -R plexbot:plexbot /data

# Optional: drop a prebuilt `tdl` binary into /usr/local/bin if not bundled.
ENV PATH="/app/bin:${PATH}"

USER plexbot

ENTRYPOINT ["python", "-m", "app.telegram.main"]
