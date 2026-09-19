# n8n + FFmpeg + Python (edge-tts, gradio_client) for Render.
# The official n8nio/n8n image no longer ships a package manager (no apk),
# so we build on Debian-based Node and install n8n from npm instead.
#
# If n8n ever complains about the Node version, bump NODE_VERSION.
# After the first good deploy, pin N8N_VERSION (e.g. 1.xx.x) for stable rebuilds.
ARG NODE_VERSION=24
FROM node:${NODE_VERSION}-bookworm-slim

ARG N8N_VERSION=latest

USER root

# System tools: ffmpeg, python (+venv), tini (proper PID 1), curl, tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-venv python3-pip \
      curl ca-certificates tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# n8n itself (build-essential is only needed if a native module has no prebuilt binary)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && npm install -g --omit=dev n8n@${N8N_VERSION} \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv for our Python tools
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir edge-tts gradio_client

# Scripts used by the workflow's Execute Command nodes
COPY render.sh /opt/scripts/render.sh
COPY instantid_generate.py /opt/scripts/instantid_generate.py
RUN chmod +x /opt/scripts/render.sh /opt/scripts/instantid_generate.py

# Build-time smoke test: fail the build now, not at 2am on a scheduled run.
RUN node --version && \
    n8n --version && \
    ffmpeg -version | head -n 1 && \
    ffprobe -version | head -n 1 && \
    /opt/venv/bin/python3 --version && \
    /opt/venv/bin/pip show edge-tts | head -n 2 && \
    /opt/venv/bin/python3 -c "import gradio_client; print('gradio_client OK')"

RUN mkdir -p /home/node/.n8n && chown -R node:node /home/node
USER node
WORKDIR /home/node
EXPOSE 5678

ENTRYPOINT ["tini", "--"]
CMD ["n8n"]
