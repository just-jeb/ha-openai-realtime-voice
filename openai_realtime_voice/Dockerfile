# Use Home Assistant base image (required for Home Assistant addons)
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM ${BUILD_FROM}

SHELL ["/bin/sh", "-o", "pipefail", "-c"]

RUN apk add --no-cache bash python3 py3-pip

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY app/ /app/
COPY root/ /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
