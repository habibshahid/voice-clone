#!/bin/bash
# Start script for multi-voice TTS system with license auto-accept

# Environment variable for license acceptance
export COQUI_TOS_AGREED=1

# Create logs directory
mkdir -p /app/logs

# Start multiple voice servers
echo "Starting multiple voice servers..."
/app/custom/multi_voice_launcher.sh

# Wait for voice services to initialize (give them 10 seconds to start)
echo "Waiting for voice services to initialize..."
sleep 10

# Start voice dispatcher on the main port
echo "Starting voice dispatcher..."
COQUI_TOS_AGREED=1 python3 /app/custom/voice_dispatcher.py \
  --services-file /app/logs/voice_services.json \
  --port 5002 \
  --timeout 180 \
  --cache-dir /tmp/voice-cache \
  > /app/logs/voice_dispatcher.log 2>&1 &

# Keep container running
echo "Services started, container will remain running"
tail -f /app/logs/voice_dispatcher.log
