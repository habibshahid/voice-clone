#!/bin/bash
# Installer for the advanced multi-fallback TTS system
# This script sets up multiple TTS services with fallback capabilities

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
mkdir -p "/tmp/tts-fallback-cache"

echo "===== Installing Advanced Multi-Fallback TTS System ====="

# Step 1: Install additional dependencies
echo "Installing additional Python dependencies..."
apt-get update
apt-get install -y python3-psutil

source /opt/tts-asterisk-env/bin/activate
pip install psutil

# Step 2: Install the multi-backend bridge server
echo "Installing multi-backend bridge server..."
cp multi-backend-bridge.py "${SERVER_DIR}/tts_http_server.py"
chmod +x "${SERVER_DIR}/tts_http_server.py"

# Step 3: Install the lightweight voice server in Docker
echo "Installing lightweight voice server..."
docker cp lightweight-voice-server.py coqui-tts:/app/custom/lightweight_voice_server.py
docker exec coqui-tts chmod +x /app/custom/lightweight_voice_server.py

# Step 4: Install the ultra-lightweight fallback server
echo "Installing ultra-lightweight fallback server..."
cp fallback-tts-server.py "${SERVER_DIR}/fallback_tts_server.py"
chmod +x "${SERVER_DIR}/fallback_tts_server.py"

# Step 5: Create systemd service for fallback server
echo "Creating systemd service for fallback TTS server..."
cat > /etc/systemd/system/tts-fallback-server.service << EOL
[Unit]
Description=Ultra-Lightweight Fallback TTS Server
After=network.target

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=${SERVER_DIR}
ExecStart=/opt/tts-asterisk-env/bin/python3 ${SERVER_DIR}/fallback_tts_server.py --port 5004
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOL

# Step 6: Install the TTS watchdog service
echo "Installing TTS watchdog service..."
cp tts-watchdog.py "${SERVER_DIR}/tts_watchdog.py"
chmod +x "${SERVER_DIR}/tts_watchdog.py"

# Create systemd service for watchdog
cat > /etc/systemd/system/tts-watchdog.service << EOL
[Unit]
Description=TTS Service Watchdog
After=network.target docker.service tts-http-server.service tts-fallback-server.service

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

# Step 7: Create runtime startup script for Docker
echo "Creating startup script for Docker container..."
cat > "${CUSTOM_DIR}/start_tts_services.sh" << 'EOL'
#!/bin/bash
# Start script for TTS services inside Docker container

# Start primary service (original XTTS v2 with voice cloning)
echo "Starting primary TTS service (XTTS v2 with voice cloning)..."
if [ -d "/app/voice_samples" ]; then
  # Find a voice sample directory to use
  VOICE_DIR=""
  for dir in /app/voice_samples/*/; do
    if [ -d "$dir" ] && [ "$(ls -A $dir)" ]; then
      VOICE_DIR="$dir"
      break
    fi
  done
  
  if [ -n "$VOICE_DIR" ]; then
    # Find speaker samples
    SAMPLES=$(find "$VOICE_DIR" -name "*.wav" | head -n 3)
    if [ -n "$SAMPLES" ]; then
      # Start the service
      echo "Using voice samples from $VOICE_DIR"
      SAMPLE_ARGS=""
      while IFS= read -r sample; do
        SAMPLE_ARGS="$SAMPLE_ARGS --speaker_wav $sample"
      done <<< "$SAMPLES"
      
      # Start in background
      nohup python3 -m TTS.server.server \
        --model_name "tts_models/multilingual/multi-dataset/xtts_v2" \
        $SAMPLE_ARGS \
        --language_idx "en" \
        --port 5002 > /app/tts_primary.log 2>&1 &
      
      echo "Primary TTS service started on port 5002"
    else
      echo "No WAV samples found in $VOICE_DIR"
    fi
  else
    echo "No voice sample directories found"
  fi
else
  echo "Voice samples directory not found"
fi

# Start lightweight service on a different port
echo "Starting lightweight TTS service..."
if [ -d "/app/voice_samples" ]; then
  # Find a voice sample directory to use
  VOICE_DIR=""
  for dir in /app/voice_samples/*/; do
    if [ -d "$dir" ] && [ "$(ls -A $dir)" ]; then
      VOICE_DIR="$dir"
      break
    fi
  done
  
  if [ -n "$VOICE_DIR" ]; then
    # Find speaker samples (just use one for lightweight service)
    SAMPLE=$(find "$VOICE_DIR" -name "*.wav" | head -n 1)
    if [ -n "$SAMPLE" ]; then
      # Start the service
      echo "Using voice sample from $VOICE_DIR for lightweight service"
      
      # Start in background
      nohup python3 /app/custom/lightweight_voice_server.py \
        --speaker_wav "$SAMPLE" \
        --language "en" \
        --port 5003 > /app/tts_lightweight.log 2>&1 &
      
      echo "Lightweight TTS service started on port 5003"
    else
      echo "No WAV samples found in $VOICE_DIR"
    fi
  else
    echo "No voice sample directories found"
  fi
else
  echo "Voice samples directory not found"
fi

# Keep container running
echo "Services started. Container will remain running."
tail -f /app/tts_primary.log
EOL

chmod +x "${CUSTOM_DIR}/start_tts_services.sh"
docker cp "${CUSTOM_DIR}/start_tts_services.sh" coqui-tts:/app/start_tts_services.sh

# Step 8: Update Docker container configuration
echo "Updating Docker container configuration..."
sed -i 's/command: \["while true; do sleep 3600; done"\]/command: \["bash", "\/app\/start_tts_services.sh"\]/' "${INSTALL_DIR}/docker-compose.yml"

# Step 9: Restart all services
echo "Restarting services..."
systemctl daemon-reload
systemctl enable tts-fallback-server
systemctl start tts-fallback-server
systemctl enable tts-watchdog
systemctl start tts-watchdog
systemctl restart tts-http-server

# Step 10: Restart Docker container
echo "Restarting TTS Docker container..."
docker-compose -f "${INSTALL_DIR}/docker-compose.yml" down
docker-compose -f "${INSTALL_DIR}/docker-compose.yml" up -d

echo "===== Installation Complete ====="
echo ""
echo "The following components were installed:"
echo "1. Multi-backend bridge server (running on port 5003)"
echo "2. Lightweight voice server (running in Docker on port 5003)"
echo "3. Ultra-lightweight fallback server (running on host on port 5004)"
echo "4. TTS watchdog service"
echo ""
echo "The system now uses a three-tier approach:"
echo "- Primary: XTTS v2 with voice cloning (best quality, slowest)"
echo "- Secondary: Lightweight synthesizer (medium quality, faster)"
echo "- Fallback: Ultra-lightweight TTS (basic quality, fastest)"
echo ""
echo "If any service fails or times out, the system will automatically"
echo "fall back to the next available service."
echo ""
echo "You can check the system status with:"
echo "  curl http://localhost:5003/health"
echo ""
echo "The system has been configured to start automatically on boot."