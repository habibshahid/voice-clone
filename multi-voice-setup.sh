#!/bin/bash
# Setup script for Multiple Voices TTS System
# This script sets up the multi-voice system for Coqui TTS

# Detect if script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root or with sudo"
  exit 1
fi

# Configuration
INSTALL_DIR="/opt/asterisk-tts-cloning"
CUSTOM_DIR="${INSTALL_DIR}/custom"
SERVER_DIR="${INSTALL_DIR}/server"
LOGS_DIR="/var/log/tts-voices"

# Create directories if they don't exist
mkdir -p "${CUSTOM_DIR}"
mkdir -p "${SERVER_DIR}"
mkdir -p "${LOGS_DIR}"
mkdir -p "/tmp/asterisk-tts-cache"

echo "===== Installing Multi-Voice TTS System ====="


# Step 3: Create Docker startup script
echo "Creating startup script for Docker container..."
cat > "${CUSTOM_DIR}/start_multi_voice.sh" << 'EOL'
#!/bin/bash
# Start script for multi-voice TTS system

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
python3 /app/custom/voice_dispatcher.py \
  --services-file /app/logs/voice_services.json \
  --port 5002 \
  --timeout 180 \
  --cache-dir /tmp/voice-cache \
  > /app/logs/voice_dispatcher.log 2>&1 &

# Keep container running
echo "Services started, container will remain running"
tail -f /app/logs/voice_dispatcher.log
EOL

# Copy startup script to Docker container
docker cp "${CUSTOM_DIR}/start_multi_voice.sh" coqui-tts:/app/start_multi_voice.sh
docker exec coqui-tts chmod +x /app/start_multi_voice.sh

# Step 4: Update Docker container configuration
echo "Updating Docker container configuration to use multi-voice system..."
sed -i 's/command: \["bash", "\/app\/start_tts_services.sh"\]/command: \["bash", "\/app\/start_multi_voice.sh"\]/' "${INSTALL_DIR}/docker-compose.yml"

# Step 5: Bridge server update (if needed)
echo "Checking bridge server configuration..."
if [ -f "${SERVER_DIR}/tts_http_server.py" ]; then
    echo "Updating bridge server with long timeout setting..."
    sed -i 's/timeout=[0-9]\+/timeout=300/g' "${SERVER_DIR}/tts_http_server.py"
    systemctl restart tts-http-server
fi

# Step 6: Restart Docker container
echo "Restarting Docker container to apply changes..."
cd "${INSTALL_DIR}"
docker-compose down
docker-compose up --build -d

echo "===== Multi-Voice TTS System Installation Complete ====="
echo ""
echo "The system has been configured to:"
echo "1. Launch multiple voice servers (one per voice directory)"
echo "2. Run a voice dispatcher that routes requests to the appropriate voice"
echo ""
echo "To check which voices are available, use:"
echo "  curl http://localhost:5002/api/voices"
echo ""
echo "To use a specific voice in your TTS requests, add the 'voice' parameter:"
echo "  curl -X POST -H 'Content-Type: application/json' -d '{\"text\":\"Hello\", \"voice\":\"voice001\"}' http://localhost:5002/api/tts"
echo ""
echo "To monitor the logs:"
echo "  docker exec coqui-tts cat /app/logs/voice_dispatcher.log"
echo "  docker exec coqui-tts ls -la /app/logs/"
echo ""
echo "Your Asterisk integration will continue to work as before,"
echo "but now you can select different voices!"