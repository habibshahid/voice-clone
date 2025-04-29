# Dockerfile
FROM ghcr.io/coqui-ai/tts:latest

# Install sox, ffmpeg, and nodejs
RUN apt-get update && apt-get install -y \
    sox \
    ffmpeg \
    nodejs \
    npm \
    && apt-get clean

# (Optional) Print installed versions
RUN sox --version && ffmpeg -version && node --version

# Default entrypoint
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["while true; do sleep 3600; done"]
