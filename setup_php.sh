#!/bin/bash
# Setup script for the Coqui TTS Voice Cloning with Asterisk (PHP AGI version)

# Detect if script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root or with sudo"
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
apt-get update
apt-get install -y docker.io docker-compose python3-pip sox ffmpeg python3-venv php php-curl

# Download PHPAGI library
echo "Installing PHPAGI library..."
mkdir -p /usr/share/phpagi
wget -O /usr/share/phpagi/phpagi.php http://sourceforge.net/projects/phpagi/files/latest/download
ln -s /usr/share/phpagi/phpagi.php /var/lib/asterisk/agi-bin/phpagi.php

# Create a Python virtual environment for the host tools
echo "Setting up Python environment..."
python3 -m venv /opt/tts-asterisk-env
source /opt/tts-asterisk-env/bin/activate
pip install flask requests

# Install the HTTP server as a service
echo "Setting up TTS HTTP service..."
cat > /etc/systemd/system/tts-http-server.service << EOL
[Unit]
Description=TTS HTTP Server for Asterisk
After=network.target docker.service

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=/opt/asterisk-tts-cloning
ExecStart=/opt/tts-asterisk-env/bin/python3 /opt/asterisk-tts-cloning/server/tts_http_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOL

# Copy AGI script to Asterisk directory
echo "Installing Asterisk PHP AGI script..."
mkdir -p /var/lib/asterisk/agi-bin
cp tts_agi.php /var/lib/asterisk/agi-bin/
chmod +x /var/lib/asterisk/agi-bin/tts_agi.php
chown asterisk:asterisk /var/lib/asterisk/agi-bin/tts_agi.php

# Copy dialplan to Asterisk configuration
echo "Installing Asterisk dialplan..."
cp extensions_tts.conf /etc/asterisk/
chown asterisk:asterisk /etc/asterisk/extensions_tts.conf

# Add include to extensions.conf if not already there
if ! grep -q '#include "extensions_tts.conf"' /etc/asterisk/extensions.conf; then
    echo '#include "extensions_tts.conf"' >> /etc/asterisk/extensions.conf
fi

# Pull Docker image
echo "Pulling Coqui TTS Docker image..."
docker pull ghcr.io/coqui-ai/tts

# Create directories with proper permissions
echo "Setting up directories..."
mkdir -p /opt/asterisk-tts-cloning/voice_samples
mkdir -p /opt/asterisk-tts-cloning/models
mkdir -p /opt/asterisk-tts-cloning/server
mkdir -p /tmp/asterisk-tts

# Copy all files to the installation directory
echo "Copying files to installation directory..."
cp -r ./* /opt/asterisk-tts-cloning/
chown -R asterisk:asterisk /opt/asterisk-tts-cloning
chown -R asterisk:asterisk /tmp/asterisk-tts

# Start services
echo "Starting services..."
systemctl daemon-reload
systemctl enable tts-http-server
systemctl start tts-http-server

# Start Docker container
echo "Starting Docker container..."
cd /opt/asterisk-tts-cloning
docker compose up -d

# Reload Asterisk dialplan
echo "Reloading Asterisk dialplan..."
asterisk -rx "dialplan reload"

echo "=============================="
echo "Installation complete!"
echo "To clone a voice:"
echo "1. Place WAV samples in /opt/asterisk-tts-cloning/voice_samples/your_voice_name/"
echo "2. Run: cd /opt/asterisk-tts-cloning && python3 server/clone_voice.py --samples_dir voice_samples/your_voice_name --output_dir models/your_voice_name --name your_voice_name"
echo "3. Follow the instructions provided by the script"
echo "4. Test your voice by calling extension 123"
echo "=============================="
