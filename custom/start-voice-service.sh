#!/bin/bash
# Start script for the Coqui TTS Voice Cloning Service

# Default settings
VOICE_DIR=""
MODEL="tts_models/multilingual/multi-dataset/xtts_v2"
LANGUAGE="en"
LANGUAGE_VARIANT=""  # Added this line
PORT=5002
USE_GPU=false
CUSTOM_SERVER=true
MAX_SAMPLES=10

# Display usage information
function show_usage {
    echo "Usage: $0 [options]"
    echo "Options:"
    echo "  --voice VOICE_DIR       Directory containing voice samples (required)"
    echo "  --model MODEL           TTS model to use (default: $MODEL)"
    echo "  --language LANG         Language code (default: $LANGUAGE)"
    echo "  --language-variant LANG Language variant/accent (e.g., en-gb)"
    echo "  --port PORT             Server port (default: $PORT)"
    echo "  --gpu                   Use GPU if available (default: disabled)"
    echo "  --standard-server       Use standard TTS server instead of custom server"
    echo "  --max-samples NUMBER    Maximum number of voice samples to use (default: $MAX_SAMPLES)"
    echo "  --help                  Show this help message"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --voice)
            VOICE_DIR="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --language)
            LANGUAGE="$2"
            shift 2
            ;;
        --language-variant)
            LANGUAGE_VARIANT="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --gpu)
            USE_GPU=true
            shift
            ;;
        --standard-server)
            CUSTOM_SERVER=false
            shift
            ;;
        --max-samples)
            MAX_SAMPLES="$2"
            shift 2
            ;;
        --help)
            show_usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option $1"
            show_usage
            exit 1
            ;;
    esac
done

# Check if voice directory is specified
if [ -z "$VOICE_DIR" ]; then
    echo "Error: Voice directory must be specified with --voice"
    show_usage
    exit 1
fi

# Check if voice directory exists
if [ ! -d "$VOICE_DIR" ]; then
    echo "Error: Voice directory does not exist: $VOICE_DIR"
    exit 1
fi

# Find voice samples
echo "Looking for voice samples in $VOICE_DIR..."
SAMPLES=()
for ext in wav WAV mp3 MP3 flac FLAC; do
    for file in "$VOICE_DIR"/*.$ext; do
        if [ -f "$file" ]; then
            SAMPLES+=("$file")
        fi
    done
done

# Check if we have samples
if [ ${#SAMPLES[@]} -eq 0 ]; then
    echo "Error: No audio samples found in $VOICE_DIR"
    exit 1
fi

echo "Found ${#SAMPLES[@]} audio samples"

# Limit number of samples if needed
if [ ${#SAMPLES[@]} -gt $MAX_SAMPLES ]; then
    echo "Using only $MAX_SAMPLES samples (out of ${#SAMPLES[@]})"
    SAMPLES=("${SAMPLES[@]:0:$MAX_SAMPLES}")
fi

# Print sample files
echo "Using these audio samples:"
for sample in "${SAMPLES[@]}"; do
    echo "  - $(basename "$sample")"
done

# Build command
if [ "$CUSTOM_SERVER" = true ]; then
    # Custom server command
    CMD="python3 /app/custom/voice_clone_server.py"
    
    # Add samples
    SAMPLE_ARGS=""
    for sample in "${SAMPLES[@]}"; do
        SAMPLE_ARGS="$SAMPLE_ARGS $sample"
    done
    CMD="$CMD --speaker_wav$SAMPLE_ARGS"
    
    # Add other arguments
    CMD="$CMD --language $LANGUAGE --port $PORT"
    
    # Add language variant if specified
    if [ -n "$LANGUAGE_VARIANT" ]; then
        CMD="$CMD --language_variant $LANGUAGE_VARIANT"
    fi
    
    # Add GPU flag if enabled
    if [ "$USE_GPU" = true ]; then
        CMD="$CMD --gpu"
    fi
else
    # Standard TTS server command
    CMD="python3 -m TTS.server.server"
    
    # Add model
    CMD="$CMD --model_name \"$MODEL\""
    
    # Add samples
    SAMPLE_ARGS=""
    for sample in "${SAMPLES[@]}"; do
        SAMPLE_ARGS="$SAMPLE_ARGS \"$sample\""
    done
    CMD="$CMD --speaker_wav$SAMPLE_ARGS"
    
    # Add other arguments
    CMD="$CMD --language_idx \"$LANGUAGE\" --port $PORT"
    
    # Add language variant if specified (if supported by standard server)
    if [ -n "$LANGUAGE_VARIANT" ]; then
        CMD="$CMD --language_variant \"$LANGUAGE_VARIANT\""
    fi
fi

# Print the command
echo "Starting voice cloning server with command:"
echo "$CMD"
echo

# Execute the command
eval "$CMD"