#!/bin/bash
# Setup script for Multiple Voices TTS System with License Auto-Accept
# This script sets up the multi-voice system for Coqui TTS and handles license acknowledgment

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
mkdir -p "/tmp/voice-cache"

echo "===== Installing Multi-Voice TTS System with License Auto-Accept ====="

# Step 1: Install jq for JSON processing
echo "Installing dependencies..."
apt-get update
apt-get install -y jq ffmpeg netcat-openbsd

# Step 2: Create model downloader script
echo "Creating model downloader script..."
cat > "${CUSTOM_DIR}/download_model.py" << 'EOL'
#!/usr/bin/env python3
"""
Interactive model downloader for Coqui TTS
This script handles the license acknowledgment and downloads the model
"""
import os
import sys
from TTS.api import TTS

print("=== Interactive Model Downloader for Coqui TTS ===")
print("This script will download the XTTS v2 model with license acknowledgment")
print()

# Set environment variable to auto-accept the license
os.environ["COQUI_TOS_AGREED"] = "1"

print("Downloading and initializing the model...")
try:
    # Initialize TTS with the model - this will trigger the download
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    
    print("Model downloaded and initialized successfully!")
    print("You can now run the multi-voice system without prompt interruptions.")
    sys.exit(0)
    
except Exception as e:
    print(f"Error downloading model: {str(e)}")
    print("\nAlternative method: Try running the following command interactively:")
    print("python3 -c 'from TTS.api import TTS; TTS(\"tts_models/multilingual/multi-dataset/xtts_v2\")'")
    sys.exit(1)
EOL

chmod +x "${CUSTOM_DIR}/download_model.py"

# Step 6: Run the model downloader in Docker to handle license
echo "Pre-downloading model with license acknowledgment..."
docker exec coqui-tts bash -c "cd /app && COQUI_TOS_AGREED=1 python3 /app/custom/download_model.py"
if [ $? -ne 0 ]; then
    echo "WARNING: Auto-download of model failed. You may need to run it interactively."
    echo "You can try running this command:"
    echo "docker exec -it coqui-tts bash -c \"cd /app && python3 -c 'from TTS.api import TTS; TTS(\"tts_models/multilingual/multi-dataset/xtts_v2\")'\""
fi

# Step 7: Create Docker startup script
echo "Creating startup script for Docker container..."
cat > "${CUSTOM_DIR}/start_multi_voice.sh" << 'EOL'
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
EOL

# Copy startup script to Docker container
docker cp "${CUSTOM_DIR}/start_multi_voice.sh" coqui-tts:/app/start_multi_voice.sh
docker exec coqui-tts chmod +x /app/start_multi_voice.sh

# Step 8: Update Docker container configuration
echo "Updating Docker container configuration to use multi-voice system..."
sed -i 's/command: \["bash", "\/app\/start_tts_services.sh"\]/command: \["bash", "\/app\/start_multi_voice.sh"\]/' "${INSTALL_DIR}/docker-compose.yml"

# Step 9: Create a simple script to check voice service status
echo "Creating voice service status checker script..."
cat > "${CUSTOM_DIR}/check_voice_services.sh" << 'EOL'
#!/bin/bash
# Check status of voice services

echo "===== Voice Services Status ====="
echo "Checking voice dispatcher..."
if docker exec coqui-tts curl -s http://localhost:5002/health > /dev/null; then
    echo "Voice dispatcher is running on port 5002"
else
    echo "Voice dispatcher is NOT running"
fi

echo ""
echo "Checking individual voice services..."
if [ -f /tmp/voice_services.json ]; then
    docker exec coqui-tts cat /app/logs/voice_services.json > /tmp/voice_services.json
    
    # Parse the JSON and check each service
    jq -c '.[]' /tmp/voice_services.json | while read -r service; do
        name=$(echo $service | jq -r '.name')
        port=$(echo $service | jq -r '.port')
        
        if docker exec coqui-tts curl -s http://localhost:$port/health > /dev/null; then
            echo " - $name (port $port): RUNNING"
        else
            echo " - $name (port $port): NOT RUNNING"
        fi
    done
    
    rm /tmp/voice_services.json
else
    echo "Voice services configuration not found"
fi

echo ""
echo "Recent log entries:"
docker exec coqui-tts tail -n 20 /app/logs/voice_dispatcher.log
EOL

chmod +x "${CUSTOM_DIR}/check_voice_services.sh"

# Step 10: Bridge server update (if needed)
echo "Checking bridge server configuration..."
if [ -f "${SERVER_DIR}/tts_http_server.py" ]; then
    echo "Updating bridge server with long timeout setting..."
    sed -i 's/timeout=[0-9]\+/timeout=300/g' "${SERVER_DIR}/tts_http_server.py"
    systemctl restart tts-http-server
fi

# Step 11: Restart Docker container
echo "Restarting Docker container to apply changes..."
cd "${INSTALL_DIR}"
docker-compose down
docker-compose up --build -d

echo "===== Multi-Voice TTS System Installation Complete ====="
echo ""
echo "The system has been configured to:"
echo "1. Auto-accept the license for XTTS v2 model"
echo "2. Launch multiple voice servers (one per voice directory)"
echo "3. Run a voice dispatcher that routes requests to the appropriate voice"
echo ""
echo "To check which voices are available, use:"
echo "  curl http://localhost:5002/api/voices"
echo ""
echo "To use a specific voice in your TTS requests, add the 'voice' parameter:"
echo "  curl -X POST -H 'Content-Type: application/json' -d '{\"text\":\"Hello\", \"voice\":\"voice001\"}' http://localhost:5002/api/tts"
echo ""
echo "To monitor the status of voice services:"
echo "  ${CUSTOM_DIR}/check_voice_services.sh"
echo ""
echo "To check logs:"
echo "  docker exec coqui-tts cat /app/logs/voice_dispatcher.log"
echo "  docker exec coqui-tts ls -la /app/logs/"
echo ""
echo "If you're still having license acceptance issues, you may need to run this command:"
echo "  docker exec -it coqui-tts bash -c \"cd /app && COQUI_TOS_AGREED=1 python3 -c 'from TTS.api import TTS; TTS(\\\"tts_models/multilingual/multi-dataset/xtts_v2\\\")'\""
echo ""
echo "Your Asterisk integration will continue to work as before,"
echo "but now you can select different voices!"