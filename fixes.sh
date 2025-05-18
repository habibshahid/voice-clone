#!/bin/bash
# Installer for the timeout fixes for the TTS Voice Cloning system

# Detect if script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root or with sudo"
  exit 1
fi

# Installation directory
INSTALL_DIR="/opt/asterisk-tts-cloning"
CUSTOM_DIR="${INSTALL_DIR}/custom"
SERVER_DIR="${INSTALL_DIR}/server"

# Create directories if they don't exist
mkdir -p "${CUSTOM_DIR}"
mkdir -p "${SERVER_DIR}"

echo "===== Installing TTS Timeout Fixes ====="

# Step 1: Install additional dependencies
echo "Installing additional Python dependencies..."
apt-get update
apt-get install -y python3-psutil

source /opt/tts-asterisk-env/bin/activate
pip install psutil

# Step 2: Install the improved bridge server
echo "Installing improved HTTP bridge server..."
cp timeout-fix-bridge-server.py "${SERVER_DIR}/tts_http_server.py"
chmod +x "${SERVER_DIR}/tts_http_server.py"

# Step 3: Install the improved voice clone server in Docker
echo "Installing improved voice clone server..."
docker cp timeout-fix-voice-server.py coqui-tts:/app/custom/voice_clone_server.py
docker exec coqui-tts chmod +x /app/custom/voice_clone_server.py

# Step 4: Install the audio preprocessing script
echo "Installing audio preprocessing script..."
docker cp preprocess_audio.py coqui-tts:/app/custom/preprocess_audio.py
docker exec coqui-tts chmod +x /app/custom/preprocess_audio.py

# Step 5: Install the voice sample collector
echo "Installing voice sample collector..."
docker cp voice_sample_collector.py coqui-tts:/app/custom/voice_sample_collector.py
docker exec coqui-tts chmod +x /app/custom/voice_sample_collector.py

# Step 6: Install the start script
echo "Installing voice service start script..."
docker cp start_voice_service.sh coqui-tts:/app/custom/start_voice_service.sh
docker exec coqui-tts chmod +x /app/custom/start_voice_service.sh

# Step 7: Install the watchdog service
echo "Installing TTS watchdog service..."
cp tts-watchdog.py "${SERVER_DIR}/tts_watchdog.py"
chmod +x "${SERVER_DIR}/tts_watchdog.py"

# Create systemd service for watchdog
cat > /etc/systemd/system/tts-watchdog.service << EOL
[Unit]
Description=TTS Service Watchdog
After=network.target docker.service tts-http-server.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=${SERVER_DIR}
ExecStart=/opt/tts-asterisk-env/bin/python3 ${SERVER_DIR}/tts_watchdog.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOL

# Step 8: Restart services
echo "Restarting services..."
systemctl daemon-reload
systemctl restart tts-http-server
systemctl enable tts-watchdog
systemctl start tts-watchdog

# Step 9: Restart Docker container
echo "Restarting TTS Docker container..."
docker restart coqui-tts

echo "===== Installation Complete ====="
echo ""
echo "The following components were installed:"
echo "1. Improved HTTP bridge server"
echo "2. Enhanced voice clone server"
echo "3. Audio preprocessing tools"
echo "4. Voice sample collection tools"
echo "5. TTS service start script"
echo "6. TTS watchdog service"
echo ""
echo "To use the improved voice clone server, connect to the Docker container and run:"
echo "  /app/custom/start_voice_service.sh --voice /app/voice_samples/your_voice_dir"
echo ""
echo "To extract high-quality voice samples:"
echo "  docker exec -it coqui-tts /app/custom/voice_sample_collector.py --input /path/to/recording.mp3 --output_dir /app/voice_samples/extracted_samples"
echo ""
echo "To preprocess audio samples:"
echo "  docker exec -it coqui-tts /app/custom/preprocess_audio.py --input_dir /app/voice_samples/raw_samples --output_dir /app/voice_samples/processed_samples"
echo ""
echo "All services have been restarted and should now be operational with improved timeout handling."