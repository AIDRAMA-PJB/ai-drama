# Custom n8n image for Render: adds FFmpeg + Python + edge-tts + gradio_client
# (Hugging Face Spaces client) on top of the official n8n image.
#
# After the first successful deploy, check the n8n version shown in the UI and
# pin it here (e.g. ARG N8N_VERSION=1.xx.x) so rebuilds stay predictable.
ARG N8N_VERSION=latest
FROM n8nio/n8n:${N8N_VERSION}

USER root

# The n8n base image is Alpine-based, so packages come from apk.
RUN apk update && apk add --no-cache \
    ffmpeg \
    python3 \
    py3-pip \
    bash \
    curl

# Isolated venv for our Python tools (Alpine's system pip is externally-managed)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir edge-tts gradio_client

# Scripts used by the workflow's Execute Command nodes
COPY render.sh /opt/scripts/render.sh
COPY instantid_generate.py /opt/scripts/instantid_generate.py
RUN chmod +x /opt/scripts/render.sh /opt/scripts/instantid_generate.py

# Build-time smoke test: fail the build now instead of at 2am on a scheduled run.
RUN ffmpeg -version | head -n 1 && \
    ffprobe -version | head -n 1 && \
    /opt/venv/bin/python3 --version && \
    /opt/venv/bin/pip show edge-tts | head -n 2 && \
    /opt/venv/bin/python3 -c "import gradio_client; print('gradio_client OK')"

USER node
