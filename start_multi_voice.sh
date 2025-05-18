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