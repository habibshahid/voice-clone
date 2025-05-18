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
