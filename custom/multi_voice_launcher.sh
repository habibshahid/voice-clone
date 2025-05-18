#!/bin/bash
# Multi-Voice Launcher for Coqui TTS with Enhanced Voice Clone Server
# This script launches multiple instances of the enhanced voice clone server, 
# each with different voice samples, and generates properly formatted JSON

# Configuration
BASE_PORT=5010      # Starting port number
MAX_VOICES=5        # Maximum number of voice services to start
SAMPLES_DIR="/app/voice_samples"
SERVER_SCRIPT="/app/custom/voice_clone_server.py"
LOG_DIR="/app/logs"
USE_GPU=false       # Set to true if you want to use GPU
LANGUAGE="en"       # Default language

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Set environment variable to auto-accept the license
export COQUI_TOS_AGREED=1

# Function to check if a port is available
check_port() {
    local port=$1
    nc -z localhost $port >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        # Port is in use
        return 1
    else
        # Port is available
        return 0
    fi
}

# Function to find available port starting from a base port
find_available_port() {
    local port=$1
    while ! check_port $port; do
        port=$((port + 1))
    done
    echo $port
}

# Function to pre-initialize the model to handle license
pre_initialize_model() {
    echo "Pre-initializing TTS model to handle license acknowledgment..."
    
    # Create a simple Python script to initialize the model
    cat > /tmp/initialize_model.py << EOF
from TTS.api import TTS
import os
os.environ["COQUI_TOS_AGREED"] = "1"
print("Initializing TTS model...")
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
print("Model initialized successfully!")
EOF
    
    # Run the initialization script
    python3 /tmp/initialize_model.py > "$LOG_DIR/model_init.log" 2>&1
    
    # Check if initialization was successful
    if grep -q "Model initialized successfully" "$LOG_DIR/model_init.log"; then
        echo "Model pre-initialization successful!"
        return 0
    else
        echo "Model pre-initialization failed. See $LOG_DIR/model_init.log for details."
        cat "$LOG_DIR/model_init.log"
        return 1
    fi
}

# Function to start a voice server with proper JSON formatting
start_voice_server() {
    local voice_dir=$1
    local voice_name=$(basename "$voice_dir")
    local port=$(find_available_port $BASE_PORT)
    BASE_PORT=$((port + 1))  # Update for next server
    
    # Find WAV files
    local wav_files=()
    while IFS= read -r file; do
        wav_files+=("$file")
    done < <(find "$voice_dir" -name "*.wav" | head -n 5)
    
    # Check if we have sample files
    if [ ${#wav_files[@]} -eq 0 ]; then
        echo "No WAV files found in $voice_dir, skipping..."
        return
    fi
    
    # Build speaker_wav arguments
    local speaker_args=""
    for wav in "${wav_files[@]}"; do
        speaker_args="$speaker_args --speaker_wav $wav"
    done
    
    # Prepare GPU argument if enabled
    local gpu_arg=""
    if [ "$USE_GPU" = true ]; then
        gpu_arg="--gpu"
    fi
    
    # Create a unique cache directory for this voice
    local cache_dir="/tmp/tts_cache_$voice_name"
    mkdir -p "$cache_dir"
    
    # Start the server
    echo "Starting voice service for '$voice_name' on port $port with ${#wav_files[@]} samples..."
    
    # Build the full command
    local cmd="python3 $SERVER_SCRIPT $speaker_args --language $LANGUAGE --port $port --cache_dir $cache_dir --voice_name $voice_name $gpu_arg"
    
    # Log the command
    echo "$(date): Starting $voice_name with command: $cmd" >> "$LOG_DIR/voice_services.log"
    
    # Run the command in background with process prioritization
    eval "COQUI_TOS_AGREED=1 nice -n 10 ionice -c2 -n7 setsid $cmd > \"$LOG_DIR/$voice_name-$port.log\" 2>&1 &"
    local pid=$!
    
    echo "  Service started on port $port (PID $pid)"
    echo "  Logs available at $LOG_DIR/$voice_name-$port.log"
    
    # Add service info to global array
    services+=("{\"name\":\"$voice_name\",\"port\":$port,\"samples\":${#wav_files[@]},\"pid\":$pid}")
    
    # Give the server some time to start
    sleep 15  # Increased sleep time to allow model loading
    
    # Check if process is still running
    if kill -0 $pid 2>/dev/null; then
        echo "  Service is running (verified)"
        
        # Check if health endpoint responds
        if curl -s "http://localhost:$port/health" > /dev/null; then
            echo "  Health endpoint verified"
        else
            echo "  WARNING: Health endpoint not responding"
        fi
    else
        echo "  WARNING: Service may have failed to start, check logs at $LOG_DIR/$voice_name-$port.log"
        echo "  Last 10 lines of log:"
        tail -n 10 "$LOG_DIR/$voice_name-$port.log"
    fi
}

# Pre-initialize the model to handle license
pre_initialize_model || { echo "Model initialization failed, exiting."; exit 1; }

# Create a properly formatted JSON file - use an array to collect entries
services=()

# Main script
echo "===== Starting Multiple Voice Services ====="

# Check if the server script exists
if [ ! -f "$SERVER_SCRIPT" ]; then
    echo "Error: Server script not found at $SERVER_SCRIPT"
    exit 1
fi

# Check if samples directory exists
if [ ! -d "$SAMPLES_DIR" ]; then
    echo "Error: Samples directory not found at $SAMPLES_DIR"
    exit 1
fi

# Find voice directories
voice_dirs=()
for dir in "$SAMPLES_DIR"/*/; do
    if [ -d "$dir" ]; then
        voice_dirs+=("$dir")
    fi
done

# Check if we found any voice directories
if [ ${#voice_dirs[@]} -eq 0 ]; then
    echo "No voice sample directories found in $SAMPLES_DIR"
    # Create an empty JSON array
    echo "[]" > "$LOG_DIR/voice_services.json"
    exit 1
fi

echo "Found ${#voice_dirs[@]} voice directories:"
for dir in "${voice_dirs[@]}"; do
    echo "  - $(basename "$dir")"
done

# Limit the number of voices if needed
if [ ${#voice_dirs[@]} -gt $MAX_VOICES ]; then
    echo "Limiting to $MAX_VOICES voices (out of ${#voice_dirs[@]} found)"
    voice_dirs=("${voice_dirs[@]:0:$MAX_VOICES}")
fi

# Start a server for each voice and collect in the services array
echo "Starting voice services..."
for dir in "${voice_dirs[@]}"; do
    start_voice_server "$dir"
    # Add extra delay between starting voice services
    echo "Waiting for service to stabilize before starting next one..."
    sleep 5
done

# Now create the properly formatted JSON file with commas between objects
echo "[" > "$LOG_DIR/voice_services.json"
for i in "${!services[@]}"; do
    if [ $i -gt 0 ]; then
        echo "," >> "$LOG_DIR/voice_services.json"
    fi
    echo "${services[$i]}" >> "$LOG_DIR/voice_services.json"
done
echo "]" >> "$LOG_DIR/voice_services.json"

# Verify JSON is valid
echo "Validating JSON file..."
jq '.' "$LOG_DIR/voice_services.json" > /dev/null
if [ $? -eq 0 ]; then
    echo "JSON file is valid."
else
    echo "ERROR: Generated JSON file is invalid!"
    cat "$LOG_DIR/voice_services.json"
fi

echo "===== All Voice Services Started ====="
echo "Services configuration saved to $LOG_DIR/voice_services.json"
echo ""
echo "To stop all services:"
echo "  pkill -f \"python3 $SERVER_SCRIPT\""
echo ""

# List all voice services with their status
echo "Voice service status:"
for i in "${!services[@]}"; do
    # Extract pid from the service JSON
    pid=$(echo "${services[$i]}" | grep -o '"pid":[0-9]*' | cut -d: -f2)
    name=$(echo "${services[$i]}" | grep -o '"name":"[^"]*"' | cut -d: -f2 | tr -d '"')
    port=$(echo "${services[$i]}" | grep -o '"port":[0-9]*' | cut -d: -f2)
    
    if kill -0 $pid 2>/dev/null; then
        status="RUNNING"
    else
        status="FAILED"
    fi
    
    echo "  - $name (port $port): $status (PID $pid)"
done