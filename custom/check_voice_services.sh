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
