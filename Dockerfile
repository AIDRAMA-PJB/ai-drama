# n8n only, for Render.
# AI drama tooling (ffmpeg, python, edge-tts, gradio_client) removed --
# that workflow is archived and this instance only runs lead-generation now.
# Using the official n8nio/n8n image directly: it's purpose-built and lighter
# than a generic Debian base, which is what we needed before only because we
# had to apt-install ffmpeg/python (no longer required).
#
# Pin the version after your first good deploy for stable rebuilds,
# e.g. FROM n8nio/n8n:1.xx.x
FROM n8nio/n8n:latest

EXPOSE 5678
