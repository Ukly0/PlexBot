FROM python:3.11-slim

ARG TDL_VERSION=0.18.4

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN sed -i 's/^deb \(.*\)/deb \1 non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        unrar \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L "https://github.com/iyear/tdl/releases/download/v${TDL_VERSION}/tdl_Linux_64bit.tar.gz" \
    | tar xz -C /usr/local/bin tdl \
    && chmod +x /usr/local/bin/tdl

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

RUN useradd -m plexbot \
    && mkdir -p /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.bot"]