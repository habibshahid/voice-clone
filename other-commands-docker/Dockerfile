FROM ghcr.io/coqui-ai/tts:latest

# Install required tools
RUN apt-get update && \
    apt-get install -y sox ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy your custom server script
COPY voice_clone_server.py /app/custom/voice_clone_server.py
RUN chmod +x /app/custom/voice_clone_server.py

# Set work directory
WORKDIR /app

# Default command to keep container running
CMD ["tail", "-f", "/dev/null"]
